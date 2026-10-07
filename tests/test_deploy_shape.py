"""Whether each capability in this fleet survives a rollout and has a capacity lever.

Per-server deploy tests cannot see a property identical across all servers, such as a single
replica with no autoscaler, disruption budget or spread, which makes every drain or rollout a
full outage. The grace period is derived from each manifest's `request_timeout` (the longest a
caller waits, and the longest an in-flight call is worth finishing, since nothing is persisted
before the response) plus a fixed drain. The server list is read from the filesystem, so a new
server is covered the day its directory exists.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp_server_kit.rebinding import ALLOWED_HOSTS_ENV, parse_allowed_hosts

ROOT = Path(__file__).resolve().parents[1]
SERVERS = ROOT / "servers"

# Seconds allowed for the endpoint-removal to propagate and for uvicorn to stop accepting, on top of
# the manifest's own `request_timeout`. One number for the fleet because it is a property of
# Kubernetes and the transport rather than of any server.
DRAIN_SECONDS = 30


def server_dirs() -> list[Path]:
    """Every server directory — a subdirectory of `servers/` holding a `connector.yaml`."""
    return sorted(path for path in SERVERS.iterdir() if (path / "connector.yaml").is_file())


def _load(path: Path) -> dict[str, Any]:
    """One parsed manifest, asserted to be a mapping so a mis-indented file fails loudly."""
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{path} did not parse as a mapping"
    return loaded


def _deployment(server: Path) -> dict[str, Any]:
    """The parsed Deployment for one server."""
    return _load(server / "deploy" / "deployment.yaml")


def _pod_spec(server: Path) -> dict[str, Any]:
    """The pod template's spec — where the disruption-survival fields live."""
    spec = _deployment(server)["spec"]["template"]["spec"]
    assert isinstance(spec, dict)
    return spec


def _request_timeout(server: Path) -> int:
    """The manifest's declared caller budget, in seconds.

    Read from the text so a manifest that stops declaring it fails here naming the file, not with a
    distant `KeyError`.
    """
    text = (server / "connector.yaml").read_text(encoding="utf-8")
    match = re.search(r"^\s*request_timeout:\s*(\d+)\s*$", text, re.M)
    assert match, f"{server.name}/connector.yaml declares no request_timeout"
    return int(match.group(1))


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_a_capability_is_never_one_pod(server: Path) -> None:
    """`replicas: 1` is a single point of failure for every user of that capability.

    Checked as a floor rather than an equality: `hpa.yaml` owns the number above it, and a server
    that legitimately wants three baseline pods should not have to edit this test.
    """
    replicas = _deployment(server)["spec"]["replicas"]
    assert isinstance(replicas, int) and replicas >= 2, (
        f"{server.name} ships replicas={replicas!r}: a rollout, a drain or one failed liveness "
        "probe then takes the whole capability offline, and there is no second pod to serve"
    )


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_the_grace_period_is_derived_from_the_budget_the_manifest_declares(server: Path) -> None:
    """`terminationGracePeriodSeconds` == the manifest's `request_timeout` + the drain.

    Both directions: too short and Kubernetes SIGKILLs work a caller is still waiting for, which is
    lost because nothing is written until a call returns; too long and every rollout stalls on pods
    nobody is waiting for.
    """
    declared = _pod_spec(server).get("terminationGracePeriodSeconds")
    expected = _request_timeout(server) + DRAIN_SECONDS
    assert declared == expected, (
        f"{server.name} declares terminationGracePeriodSeconds={declared!r}; "
        f"connector.yaml's request_timeout is {_request_timeout(server)} s, so it must be "
        f"{expected}. Change the manifest's budget and this number follows it — that is the point "
        "of deriving it rather than writing it twice"
    )


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_replicas_are_spread_across_nodes(server: Path) -> None:
    """Two replicas on one node are one replica: the node is what a drain or a panic takes."""
    constraints = _pod_spec(server).get("topologySpreadConstraints")
    assert constraints, f"{server.name} has no topologySpreadConstraints"
    assert len(constraints) == 1, "one constraint, over the node"
    only = constraints[0]
    assert only["topologyKey"] == "kubernetes.io/hostname"
    assert only["maxSkew"] == 1
    # `ScheduleAnyway`, so a single-node dev or CI cluster still schedules the second pod. A
    # `DoNotSchedule` constraint leaves it Pending there, which is how a spread constraint gets
    # deleted rather than fixed.
    assert only["whenUnsatisfiable"] == "ScheduleAnyway"
    selector = only["labelSelector"]["matchLabels"]["app.kubernetes.io/name"]
    pod_label = _deployment(server)["spec"]["template"]["metadata"]["labels"][
        "app.kubernetes.io/name"
    ]
    assert selector == pod_label, (
        f"{server.name}: the spread constraint selects {selector!r} and the pod is labelled "
        f"{pod_label!r}, so it spreads nothing — and reports no error while doing it"
    )


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_scratch_space_is_bounded(server: Path) -> None:
    """Every scratch `emptyDir` has a `sizeLimit`.

    Unbounded, it draws on the node's disk (`calc` writes long-running scratch, `pyexec` writes
    caller output), and `DiskPressure` evicts unrelated pods; with a limit the kubelet evicts the
    offending pod alone.
    """
    volumes = _pod_spec(server)["volumes"]
    for volume in volumes:
        if "emptyDir" not in volume:
            continue
        # `emptyDir:` with nothing under it parses as `None` and means the same unbounded volume as
        # `emptyDir: {}`, so the membership test is what covers both spellings — reading the value
        # and skipping a falsy one would pass the very shape this checks for.
        empty_dir = volume["emptyDir"] or {}
        assert empty_dir.get("sizeLimit"), (
            f"{server.name}: volume {volume['name']!r} is an emptyDir with no sizeLimit, so it "
            "draws on the node's ephemeral storage and its blast radius is the node"
        )


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_a_voluntary_disruption_cannot_take_the_whole_capability(server: Path) -> None:
    """A PodDisruptionBudget is the only thing that makes the eviction API wait.

    Without one, a node drain or a cluster upgrade evicts every pod of a Deployment at once, and
    `minReplicas: 2` buys nothing on the axis it was added for.
    """
    pdb = _load(server / "deploy" / "pdb.yaml")
    assert pdb["kind"] == "PodDisruptionBudget"
    spec = pdb["spec"]
    # `maxUnavailable`, not `minAvailable`: at 6 replicas `minAvailable: 1` permits a drain that
    # takes five of them together, which is the outage this object exists to prevent written as a
    # budget.
    assert spec.get("maxUnavailable") == 1, (
        f"{server.name}'s PDB does not bound disruption to one pod at a time: {spec!r}"
    )
    assert "minAvailable" not in spec, "maxUnavailable and minAvailable are mutually exclusive"
    selector = spec["selector"]["matchLabels"]["app.kubernetes.io/name"]
    pod_label = _deployment(server)["spec"]["template"]["metadata"]["labels"][
        "app.kubernetes.io/name"
    ]
    assert selector == pod_label, (
        f"{server.name}: the PDB selects {selector!r} and the pod is labelled {pod_label!r}, so it "
        "protects nothing — and a selector that matches no pod is silently satisfied"
    )


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_capacity_has_a_lever(server: Path) -> None:
    """An HPA exists, targets *this* Deployment, and its floor is the Deployment's own replicas.

    An HPA whose `minReplicas` is below `replicas` scales the baseline down on the first quiet
    minute, undoing `test_a_capability_is_never_one_pod` without touching its file.
    """
    hpa = _load(server / "deploy" / "hpa.yaml")
    assert hpa["kind"] == "HorizontalPodAutoscaler"
    assert hpa["apiVersion"] == "autoscaling/v2", "v1 cannot express behaviour or a target type"
    spec = hpa["spec"]
    target = spec["scaleTargetRef"]
    assert target["kind"] == "Deployment"
    assert target["name"] == _deployment(server)["metadata"]["name"], (
        f"{server.name}'s HPA points at {target['name']!r}, which is not its Deployment; an HPA "
        "with no target reports no error and scales nothing"
    )
    replicas = _deployment(server)["spec"]["replicas"]
    assert spec["minReplicas"] == replicas, (
        f"{server.name}: HPA minReplicas={spec['minReplicas']} against Deployment "
        f"replicas={replicas}. The HPA wins, so the Deployment's floor is decorative"
    )
    assert spec["maxReplicas"] > spec["minReplicas"], "an HPA that cannot scale up is not one"


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_the_autoscaler_reads_a_signal_the_requests_make_meaningful(server: Path) -> None:
    """CPU requests are realistic, because HPA utilization is measured against `requests.cpu`.

    At a token request one caller doing a core's work reads as many times 100%, and the autoscaler
    runs to `maxReplicas` on one request.
    """
    hpa = _load(server / "deploy" / "hpa.yaml")
    metrics = hpa["spec"]["metrics"]
    assert len(metrics) == 1, "one metric, so there is one thing to reason about"
    resource = metrics[0]["resource"]
    assert resource["name"] == "cpu"
    assert resource["target"]["type"] == "Utilization"
    assert 50 <= resource["target"]["averageUtilization"] <= 85, (
        "a target below 50% wastes half the fleet; above 85% there is no time to add a pod before "
        "the queue forms"
    )
    requests = _pod_spec(server)["containers"][0]["resources"]["requests"]
    # Kubernetes accepts `"250m"`, `"1"` and an unquoted `1` for the same field, so the parse takes
    # all three rather than assuming the spelling this fleet happens to use today.
    declared = str(requests["cpu"])
    millicores = float(declared[:-1]) if declared.endswith("m") else float(declared) * 1000
    assert millicores >= 250, (
        f"{server.name} requests {requests['cpu']} of CPU; every unit of work in this fleet is "
        "CPU-bound, so a token request makes the HPA's utilization percentage meaningless"
    )


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_liveness_and_readiness_do_not_share_a_route(server: Path) -> None:
    """Liveness and readiness use different routes, because kubelet acts on them differently.

    `/healthz` consults corpora, sandboxes and optional components, so as a liveness probe any 503
    would become a kill; a restart cannot recreate a missing file, so a pod serving ten of eleven
    predictors would crash-loop. Both paths and their inequality are asserted, as literals rather
    than imported constants, since a kubelet reads these files and not this repository's Python.
    """
    container = _pod_spec(server)["containers"][0]
    readiness = container["readinessProbe"]["httpGet"]["path"]
    liveness = container["livenessProbe"]["httpGet"]["path"]
    assert readiness == "/healthz", f"{server.name} reads readiness from {readiness!r}"
    assert liveness == "/livez", (
        f"{server.name} points livenessProbe at {liveness!r}; on /healthz a broken optional "
        "component is a restart loop rather than a pod out of rotation"
    )
    assert readiness != liveness, (
        f"{server.name} points both probes at {readiness!r}, so shedding traffic and replacing the "
        "pod are one answer again"
    )


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_mcp_admits_the_host_its_own_service_is_dialled_by(server: Path) -> None:
    """`MCP_ALLOWED_HOSTS` is this server's Service `name:port`, read off `service.yaml`.

    Upstream's DNS-rebinding guard admits only a loopback `Host`, so a caller dialling the Service
    gets 421 on `/mcp` while `/healthz` stays green. Derived from the Service, so a rename or port
    move without the allow-list fails, and parsed by the kit's own reader, so a value the pod would
    refuse at startup fails here first.
    """
    service = _load(server / "deploy" / "service.yaml")
    port = service["spec"]["ports"][0]["port"]
    expected = f"{service['metadata']['name']}:{port}"
    env = {
        entry["name"]: entry.get("value")
        for entry in _pod_spec(server)["containers"][0].get("env", [])
    }
    assert ALLOWED_HOSTS_ENV in env, (
        f"{server.name} sets no {ALLOWED_HOSTS_ENV}; every in-cluster `/mcp` call is a 421"
    )
    assert expected in parse_allowed_hosts(env[ALLOWED_HOSTS_ENV]), (
        f"{server.name} admits {env[ALLOWED_HOSTS_ENV]!r}, which does not include its own Service "
        f"address {expected!r}"
    )


def _network_policy(server: Path) -> dict[str, Any]:
    """The parsed NetworkPolicy for one server."""
    return _load(server / "deploy" / "networkpolicy.yaml")


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_the_egress_policy_denies_and_selects_the_workload(server: Path) -> None:
    """Layer 4 of the no-egress posture: a default-deny NetworkPolicy, checked fleet-wide.

    Three clauses, each an independent way for "default-deny" to be false while looking unchanged:
    `Egress` absent from `policyTypes` (the direction is not governed); a non-empty `egress:` list
    (an explicit hole; an empty list, not an absent key, is what reads as deny-all); a `podSelector`
    that matches no pod (a label drift exempts the workload). The per-server deploy tests assert the
    same, deliberately; this one binds a new server that has no such file yet. Ports, ingress peers
    and scrape wiring are per-server and checked there.
    """
    spec = _network_policy(server)["spec"]
    assert isinstance(spec, dict)

    assert "Egress" in spec["policyTypes"], (
        f"{server.name}'s NetworkPolicy does not govern Egress, so outbound traffic is "
        "unrestricted while the file still looks like a default-deny policy"
    )
    assert spec["egress"] == [], (
        f"{server.name} may reach nothing at request time; its policy permits {spec['egress']!r}"
    )

    selector = spec["podSelector"]["matchLabels"]["app.kubernetes.io/name"]
    pod_label = _deployment(server)["spec"]["template"]["metadata"]["labels"][
        "app.kubernetes.io/name"
    ]
    assert selector == pod_label, (
        f"{server.name}'s NetworkPolicy selects {selector!r} and its Deployment labels the pod "
        f"{pod_label!r}: the policy binds to no workload, which denies nothing"
    )


#: The namespaces a Prometheus in this family scrapes from: `monitoring` (kube-prometheus-stack)
#: and `openshift-user-workload-monitoring` (OpenShift's user-workload Prometheus). Written as
#: literals, since deriving them from the checked files would agree with whatever they hold.
SCRAPER_NAMESPACES = frozenset({"monitoring", "openshift-user-workload-monitoring"})


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_the_scrape_hole_admits_the_namespace_this_platform_runs_prometheus_in(
    server: Path,
) -> None:
    """The scrape ingress hole admits the namespaces the platform runs Prometheus in, and no others.

    On OpenShift the user-workload Prometheus is not in `monitoring`, so admitting only that would
    silently drop every scrape, including the fleet's egress and degradation counters. Held
    fleet-wide so a new server is bound; both directions, since admitting every namespace would also
    pass. Based on `NetworkPolicyPeer`'s documented semantics, not observed against an API server.
    """
    ingress = _network_policy(server)["spec"]["ingress"]
    selectors = [
        peer["namespaceSelector"]
        for rule in ingress
        for peer in rule.get("from", [])
        if "namespaceSelector" in peer
    ]
    assert selectors, (
        f"{server.name}'s NetworkPolicy admits no namespace at all, so its ServiceMonitor resolves "
        "a target whose pod drops the scrape and every metric this server publishes is silence"
    )

    admitted: set[str] = set()
    for selector in selectors:
        for key, value in (selector.get("matchLabels") or {}).items():
            assert key == "kubernetes.io/metadata.name", (
                f"{server.name} admits a namespace by {key!r}; the scrape peer is selected by name"
            )
            admitted.add(value)
        for expression in selector.get("matchExpressions") or []:
            assert expression["key"] == "kubernetes.io/metadata.name", expression
            assert expression["operator"] == "In", (
                f"{server.name} selects the scraper's namespace with {expression['operator']!r}; "
                "only an `In` list names which namespaces are admitted where a reader can see them"
            )
            admitted.update(expression["values"])

    assert admitted == SCRAPER_NAMESPACES, (
        f"{server.name} admits {sorted(admitted)} where this family's Prometheus runs in "
        f"{sorted(SCRAPER_NAMESPACES)}. A missing one is a scrape that is dropped and reads as a "
        "server nobody calls; an extra one is a namespace nobody argued for."
    )


def gated_server_dirs() -> list[Path]:
    """Every server with an admission gate — the ones whose occupancy is a scaling signal.

    Derived from the gate's module on disk rather than listed, so a seventh gated server owes the
    KEDA alternative the day its `engine/admission.py` exists.
    """
    return [path for path in server_dirs() if list(path.glob("src/*/engine/admission.py"))]


def _scaled_object(server: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """The ScaledObject and its TriggerAuthentication, in file order."""
    docs = list(yaml.safe_load_all((server / "deploy" / "keda" / "scaledobject.yaml").read_text()))
    kinds = [doc["kind"] for doc in docs]
    assert kinds == ["ScaledObject", "TriggerAuthentication"], f"{server.name}: {kinds}"
    return docs[0], docs[1]


@pytest.mark.parametrize("server", gated_server_dirs(), ids=lambda path: path.name)
def test_the_keda_alternative_is_the_hpa_with_a_better_signal(server: Path) -> None:
    """Swapping the CPU HPA for the KEDA ScaledObject changes the signal and nothing else.

    Same target, floor, ceiling, behaviour and CPU trigger, so adopting KEDA moves no bound held
    here; the first trigger must read *this* server's admission occupancy, not a neighbour's.
    """
    scaled, auth = _scaled_object(server)
    hpa = _load(server / "deploy" / "hpa.yaml")["spec"]
    spec = scaled["spec"]
    assert spec["scaleTargetRef"]["name"] == _deployment(server)["metadata"]["name"]
    assert spec["minReplicaCount"] == hpa["minReplicas"]
    assert spec["maxReplicaCount"] == hpa["maxReplicas"]
    assert spec["advanced"]["horizontalPodAutoscalerConfig"]["behavior"] == hpa["behavior"]

    prometheus, cpu = spec["triggers"]
    assert prometheus["type"] == "prometheus"
    query = prometheus["metadata"]["query"]
    for metric in ("chemclaw_mcp_admission_in_flight", "chemclaw_mcp_admission_ceiling"):
        assert f'{metric}{{server="{server.name}"}}' in query, f"{server.name}: {query!r}"
    assert prometheus["authenticationRef"]["name"] == auth["metadata"]["name"]
    assert cpu["type"] == "cpu"
    assert (
        int(cpu["metadata"]["value"])
        == (hpa["metrics"][0]["resource"]["target"]["averageUtilization"])
    )


def test_the_keda_alternative_is_not_applied_with_the_rest_of_deploy() -> None:
    """Two autoscalers on one Deployment fight, so the ScaledObject must be opt-in by path.

    `oc apply -f servers/<name>/deploy/` does not recurse, which is the whole mechanism: were the
    ScaledObject beside `hpa.yaml`, the ordinary apply would install both.
    """
    stray = sorted(
        str(path.relative_to(ROOT))
        for path in SERVERS.glob("*/deploy/*.yaml")
        if "ScaledObject" in path.read_text(encoding="utf-8")
    )
    assert not stray, f"a ScaledObject beside hpa.yaml is applied with it: {stray}"


# The Secret Chemclaw3's chart reads (`deploy/helm/chemclaw/values.yaml` `secrets.name`), keyed by
# variable name. The fleet runs in the release's namespace, so one Secret holds both halves of every
# bearer and the two sides cannot hold different values.
BEARER_SECRET = "chemclaw-secrets"


def _manifest_token_env(server: Path) -> str:
    """The variable the server's own `connector.yaml` declares as its bearer."""
    token_env = _load(server / "connector.yaml")["endpoint"]["auth"]["token_env"]
    assert isinstance(token_env, str) and token_env, f"{server.name} declares no token_env"
    return token_env


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_the_bearer_is_wired_from_the_secret_the_manifest_names(server: Path) -> None:
    """The Deployment hands the server exactly its manifest's `token_env`, from `chemclaw-secrets`.

    The server fails closed, so a missing bearer is a 401 on every `/mcp` call with `/healthz` at
    200. The name comes from the manifest, which both sides read. Not `optional`: a missing key
    should stop the pod starting, loudly.
    """
    token_env = _manifest_token_env(server)
    from_secrets = {
        entry["name"]: entry["valueFrom"]["secretKeyRef"]
        for entry in _pod_spec(server)["containers"][0].get("env", [])
        if "secretKeyRef" in (entry.get("valueFrom") or {})
    }
    assert set(from_secrets) == {token_env}, (
        f"{server.name} reads {sorted(from_secrets)} from a Secret; its manifest's bearer is "
        f"{token_env!r} and nothing else is a credential this server verifies"
    )
    ref = from_secrets[token_env]
    assert ref["name"] == BEARER_SECRET, (
        f"{server.name} reads its bearer from {ref['name']!r}, not {BEARER_SECRET!r}, so it and "
        "Chemclaw3 can hold different values"
    )
    assert ref["key"] == token_env, f"{server.name}: key {ref['key']!r} is not {token_env!r}"
    assert ref.get("optional", False) is False, (
        f"{server.name} marks its bearer optional, so a missing key starts a pod that refuses "
        "every call instead of one that never starts"
    )


_PINNED_IDS = ("runAsUser", "runAsGroup", "fsGroup")


def _final_stage_user_and_home(server: Path) -> tuple[str, str | None]:
    """The runtime stage's `USER`, with `ARG` defaults substituted, and its `ENV HOME`."""
    lines = (server / "Containerfile").read_text(encoding="utf-8").splitlines()
    last_from = max(i for i, line in enumerate(lines) if line.startswith("FROM "))
    args: dict[str, str] = {}
    user, home = "", None
    for line in lines[last_from:]:
        if line.startswith("ARG ") and "=" in line:
            name, _, value = line[len("ARG ") :].partition("=")
            args[name.strip()] = value.strip()
        elif line.startswith("USER "):
            user = re.sub(r"\$\{(\w+)\}", lambda m: args.get(m.group(1), m.group(0)), line[5:])
        elif line.startswith("ENV HOME="):
            home = line[len("ENV HOME=") :].strip()
    return user.strip(), home


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_the_pod_runs_under_whatever_uid_the_platform_assigns(server: Path) -> None:
    """No pinned UID, GID or fsGroup; non-root still enforced; the image verifiable as non-root.

    OpenShift's `restricted-v2` SCC assigns a UID from the namespace range and rejects pinned ones.
    `runAsNonRoot` needs a numeric image `USER` to verify, and an assigned UID has no passwd entry,
    so `HOME` must point at a writable mounted path.
    """
    spec = _pod_spec(server)
    container = spec["containers"][0]
    for where, context in (
        ("pod", spec["securityContext"]),
        ("container", container["securityContext"]),
    ):
        pinned = sorted(set(_PINNED_IDS) & set(context))
        assert not pinned, f"{server.name}: the {where} securityContext pins {pinned}"
        assert context["runAsNonRoot"] is True, f"{server.name}: {where} runAsNonRoot is not true"

    user, home = _final_stage_user_and_home(server)
    assert user.isdigit() and int(user) > 0, (
        f"{server.name}/Containerfile's runtime USER is {user!r}; only a non-zero number lets "
        "the kubelet verify runAsNonRoot"
    )
    writable = {
        mount["mountPath"]
        for mount in container.get("volumeMounts", [])
        if any(v["name"] == mount["name"] and "emptyDir" in v for v in spec.get("volumes", []))
    }
    assert home in writable, (
        f"{server.name}/Containerfile sets HOME={home!r}, which is not one of the emptyDir mounts "
        f"{sorted(writable)} — an assigned UID could not write there"
    )


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_the_image_is_the_one_placeholder_the_overlay_rewrites(server: Path) -> None:
    """Every Deployment names `registry.invalid/chemclaw-mcp-<name>:unset`, and nothing else.

    The deploy step rewrites exactly this name to a published digest (`docs/operations.md` §2,
    Chemclaw3's `deploy/kind/render-fleet.sh`); a drifted name would be applied unrewritten.
    `.invalid` never resolves (RFC 2606), so an unrewritten apply fails with `ErrImagePull` instead
    of pulling from a search registry.
    """
    image = _pod_spec(server)["containers"][0]["image"]
    expected = f"registry.invalid/chemclaw-mcp-{server.name}:unset"
    assert image == expected, f"{server.name}: {image!r}"
