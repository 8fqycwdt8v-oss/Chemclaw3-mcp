"""Whether each capability in this fleet survives a rollout and has a capacity lever.

Per-server deploy tests cannot see a property identical across all servers, such as a single
replica with no autoscaler, disruption budget or spread, which makes every drain or rollout a
full outage. Every check reads what `helm template` renders from `deploy/helm/chemclaw-fleet`
(the `fleet_render` fixture in the root `conftest.py`), never the templates or `values.yaml`, so
it holds whatever the chart's internals become. The grace period is derived from each manifest's
`request_timeout` (the longest a caller waits, and the longest an in-flight call is worth
finishing, since nothing is persisted before the response) plus a fixed drain. The server list is
read from the filesystem, so a new server is covered the day its directory exists, and the chart
must name exactly those servers.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp_server_kit.rebinding import ALLOWED_HOSTS_ENV, parse_allowed_hosts

ROOT = Path(__file__).resolve().parents[1]
SERVERS = ROOT / "servers"
CHART = ROOT / "deploy" / "helm" / "chemclaw-fleet"

Render = Callable[..., list[dict[str, Any]]]

# Seconds allowed for the endpoint-removal to propagate and for uvicorn to stop accepting, on top of
# the manifest's own `request_timeout`. One number for the fleet because it is a property of
# Kubernetes and the transport rather than of any server.
DRAIN_SECONDS = 30

KEDA = ("--set", "autoscaling.mode=keda")


def server_dirs() -> list[Path]:
    """Every server directory — a subdirectory of `servers/` holding a `connector.yaml`."""
    return sorted(path for path in SERVERS.iterdir() if (path / "connector.yaml").is_file())


def _load(path: Path) -> dict[str, Any]:
    """One parsed manifest, asserted to be a mapping so a mis-indented file fails loudly."""
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{path} did not parse as a mapping"
    return loaded


def _service_name(server: Path) -> str:
    """The name every object of this server carries, which Chemclaw3 dials."""
    return f"chemclaw-mcp-{server.name}"


def _objects(render: Render, server: Path, kind: str, *helm_args: str) -> list[dict[str, Any]]:
    """Every object of `kind` rendered for this server (by name), under the given helm arguments."""
    return [
        doc
        for doc in render(*helm_args)
        if doc["kind"] == kind and doc["metadata"]["name"] == _service_name(server)
    ]


def _object(render: Render, server: Path, kind: str, *helm_args: str) -> dict[str, Any]:
    """The one `kind` rendered for this server; zero or two is the finding."""
    found = _objects(render, server, kind, *helm_args)
    assert len(found) == 1, (
        f"the chart renders {len(found)} {kind} named {_service_name(server)} where a server owes "
        "exactly one"
    )
    return found[0]


def _deployment(render: Render, server: Path, *helm_args: str) -> dict[str, Any]:
    """The rendered Deployment for one server."""
    return _object(render, server, "Deployment", *helm_args)


def _pod_spec(render: Render, server: Path) -> dict[str, Any]:
    """The pod template's spec — where the disruption-survival fields live."""
    spec = _deployment(render, server)["spec"]["template"]["spec"]
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


def test_the_chart_names_exactly_the_servers_the_tree_has() -> None:
    """`values.yaml` lists every server directory and nothing else.

    A server with no entry is deployed by nobody and nothing is red; an entry with no directory is
    a workload for an image nobody builds.
    """
    listed = sorted(_load(CHART / "values.yaml")["servers"])
    assert listed == [path.name for path in server_dirs()], (
        "deploy/helm/chemclaw-fleet/values.yaml `servers` and servers/*/connector.yaml name "
        "different sets"
    )


KINDS = (
    "Deployment",
    "Service",
    "HorizontalPodAutoscaler",
    "PodDisruptionBudget",
    "NetworkPolicy",
    "ServiceMonitor",
)


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_a_server_is_rendered_whole(server: Path, fleet_render: Render) -> None:
    """Workload, address, autoscaler, budget, default-deny policy and scrape: one of each.

    Required fleet-wide because a server copied from a directory without one of them would not
    notice through its own tests; without the workload, every hardening field defaults to the
    cluster's.
    """
    for kind in KINDS:
        _object(fleet_render, server, kind)


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_a_capability_is_never_one_pod(server: Path, fleet_render: Render) -> None:
    """`replicas: 1` is a single point of failure for every user of that capability.

    The floor is the autoscaler's minimum (the Deployment carries no count of its own, see
    `test_an_autoscaler_owns_the_replica_count`), checked as a floor rather than an equality: a
    server that legitimately wants three baseline pods should not have to edit this test. With
    `autoscaling.mode=none` the Deployment's own count is the floor, and holds the same bar.
    """
    minimum = _object(fleet_render, server, "HorizontalPodAutoscaler")["spec"]["minReplicas"]
    fixed = _deployment(fleet_render, server, "--set", "autoscaling.mode=none")["spec"]["replicas"]
    for where, replicas in (("HPA minReplicas", minimum), ("fixed Deployment replicas", fixed)):
        assert isinstance(replicas, int) and replicas >= 2, (
            f"{server.name} ships {where}={replicas!r}: a rollout, a drain or one failed liveness "
            "probe then takes the whole capability offline, and there is no second pod to serve"
        )


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_the_grace_period_is_derived_from_the_budget_the_manifest_declares(
    server: Path, fleet_render: Render
) -> None:
    """`terminationGracePeriodSeconds` == the manifest's `request_timeout` + the drain.

    Both directions: too short and Kubernetes SIGKILLs work a caller is still waiting for, which is
    lost because nothing is written until a call returns; too long and every rollout stalls on pods
    nobody is waiting for. The chart computes it from `requestTimeout` in `values.yaml`, so a
    stale copy of the manifest's number fails here.
    """
    declared = _pod_spec(fleet_render, server).get("terminationGracePeriodSeconds")
    expected = _request_timeout(server) + DRAIN_SECONDS
    assert declared == expected, (
        f"{server.name} declares terminationGracePeriodSeconds={declared!r}; "
        f"connector.yaml's request_timeout is {_request_timeout(server)} s, so it must be "
        f"{expected}. Change the manifest's budget and `requestTimeout` in the chart's values "
        "follows it — that is the point of deriving the grace period rather than writing it twice"
    )


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_replicas_are_spread_across_nodes(server: Path, fleet_render: Render) -> None:
    """Two replicas on one node are one replica: the node is what a drain or a panic takes."""
    constraints = _pod_spec(fleet_render, server).get("topologySpreadConstraints")
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
    pod_label = _deployment(fleet_render, server)["spec"]["template"]["metadata"]["labels"][
        "app.kubernetes.io/name"
    ]
    assert selector == pod_label, (
        f"{server.name}: the spread constraint selects {selector!r} and the pod is labelled "
        f"{pod_label!r}, so it spreads nothing — and reports no error while doing it"
    )


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_scratch_space_is_bounded(server: Path, fleet_render: Render) -> None:
    """Every scratch `emptyDir` has a `sizeLimit`.

    Unbounded, it draws on the node's disk (`calc` writes long-running scratch, `pyexec` writes
    caller output), and `DiskPressure` evicts unrelated pods; with a limit the kubelet evicts the
    offending pod alone.
    """
    volumes = _pod_spec(fleet_render, server)["volumes"]
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
def test_a_voluntary_disruption_cannot_take_the_whole_capability(
    server: Path, fleet_render: Render
) -> None:
    """A PodDisruptionBudget is the only thing that makes the eviction API wait.

    Without one, a node drain or a cluster upgrade evicts every pod of a Deployment at once, and
    `minReplicas: 2` buys nothing on the axis it was added for.
    """
    pdb = _object(fleet_render, server, "PodDisruptionBudget")
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
    pod_label = _deployment(fleet_render, server)["spec"]["template"]["metadata"]["labels"][
        "app.kubernetes.io/name"
    ]
    assert selector == pod_label, (
        f"{server.name}: the PDB selects {selector!r} and the pod is labelled {pod_label!r}, so it "
        "protects nothing — and a selector that matches no pod is silently satisfied"
    )


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_capacity_has_a_lever(server: Path, fleet_render: Render) -> None:
    """An HPA exists, targets *this* Deployment, and can scale up from its floor."""
    hpa = _object(fleet_render, server, "HorizontalPodAutoscaler")
    assert hpa["kind"] == "HorizontalPodAutoscaler"
    assert hpa["apiVersion"] == "autoscaling/v2", "v1 cannot express behaviour or a target type"
    spec = hpa["spec"]
    target = spec["scaleTargetRef"]
    assert target["kind"] == "Deployment"
    assert target["name"] == _deployment(fleet_render, server)["metadata"]["name"], (
        f"{server.name}'s HPA points at {target['name']!r}, which is not its Deployment; an HPA "
        "with no target reports no error and scales nothing"
    )
    assert spec["maxReplicas"] > spec["minReplicas"], "an HPA that cannot scale up is not one"


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_an_autoscaler_owns_the_replica_count(server: Path, fleet_render: Render) -> None:
    """The Deployment carries no `spec.replicas` while an autoscaler owns the count.

    A rendered number is applied again by every `helm upgrade`, so a server scaled out to eight
    pods (calc, running CREST) would be reset to its floor, bypassing the slow scale-in window.
    Both autoscaler modes omit it; `autoscaling.mode=none` has no autoscaler and keeps the count.
    """
    for mode in ("hpa", "keda"):
        spec = _deployment(fleet_render, server, "--set", f"autoscaling.mode={mode}")["spec"]
        assert "replicas" not in spec, (
            f"{server.name} under autoscaling.mode={mode} renders replicas={spec.get('replicas')}"
            ", which the next helm upgrade applies over the autoscaler's decision"
        )
    fixed = _deployment(fleet_render, server, "--set", "autoscaling.mode=none")["spec"]
    assert fixed.get("replicas", 0) >= 2, f"{server.name}: a fixed-replica render lost its count"
    for kind in ("HorizontalPodAutoscaler", "ScaledObject"):
        assert not _objects(fleet_render, server, kind, "--set", "autoscaling.mode=none"), (
            f"{server.name} has a {kind} beside a fixed replica count, so two things own it"
        )


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_the_autoscaler_reads_a_signal_the_requests_make_meaningful(
    server: Path, fleet_render: Render
) -> None:
    """CPU requests are realistic, because HPA utilization is measured against `requests.cpu`.

    At a token request one caller doing a core's work reads as many times 100%, and the autoscaler
    runs to `maxReplicas` on one request.
    """
    hpa = _object(fleet_render, server, "HorizontalPodAutoscaler")
    metrics = hpa["spec"]["metrics"]
    assert len(metrics) == 1, "one metric, so there is one thing to reason about"
    resource = metrics[0]["resource"]
    assert resource["name"] == "cpu"
    assert resource["target"]["type"] == "Utilization"
    assert 50 <= resource["target"]["averageUtilization"] <= 85, (
        "a target below 50% wastes half the fleet; above 85% there is no time to add a pod before "
        "the queue forms"
    )
    requests = _pod_spec(fleet_render, server)["containers"][0]["resources"]["requests"]
    # Kubernetes accepts `"250m"`, `"1"` and an unquoted `1` for the same field, so the parse takes
    # all three rather than assuming the spelling this fleet happens to use today.
    declared = str(requests["cpu"])
    millicores = float(declared[:-1]) if declared.endswith("m") else float(declared) * 1000
    assert millicores >= 250, (
        f"{server.name} requests {requests['cpu']} of CPU; every unit of work in this fleet is "
        "CPU-bound, so a token request makes the HPA's utilization percentage meaningless"
    )


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_liveness_and_readiness_do_not_share_a_route(server: Path, fleet_render: Render) -> None:
    """Liveness and readiness use different routes, because kubelet acts on them differently.

    `/healthz` consults corpora, sandboxes and optional components, so as a liveness probe any 503
    would become a kill; a restart cannot recreate a missing file, so a pod serving ten of eleven
    predictors would crash-loop. Both paths and their inequality are asserted, as literals rather
    than imported constants, since a kubelet reads the rendered manifest and not this repository's
    Python.
    """
    container = _pod_spec(fleet_render, server)["containers"][0]
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
def test_mcp_admits_the_host_its_own_service_is_dialled_by(
    server: Path, fleet_render: Render
) -> None:
    """`MCP_ALLOWED_HOSTS` is this server's Service `name:port`, read off the rendered Service.

    Upstream's DNS-rebinding guard admits only a loopback `Host`, so a caller dialling the Service
    gets 421 on `/mcp` while `/healthz` stays green. Derived from the Service, so a rename or port
    move without the allow-list fails, and parsed by the kit's own reader, so a value the pod would
    refuse at startup fails here first.
    """
    service = _object(fleet_render, server, "Service")
    port = service["spec"]["ports"][0]["port"]
    expected = f"{service['metadata']['name']}:{port}"
    env = {
        entry["name"]: entry.get("value")
        for entry in _pod_spec(fleet_render, server)["containers"][0].get("env", [])
    }
    assert ALLOWED_HOSTS_ENV in env, (
        f"{server.name} sets no {ALLOWED_HOSTS_ENV}; every in-cluster `/mcp` call is a 421"
    )
    assert expected in parse_allowed_hosts(env[ALLOWED_HOSTS_ENV]), (
        f"{server.name} admits {env[ALLOWED_HOSTS_ENV]!r}, which does not include its own Service "
        f"address {expected!r}"
    )


def _network_policy(render: Render, server: Path) -> dict[str, Any]:
    """The rendered NetworkPolicy for one server."""
    return _object(render, server, "NetworkPolicy")


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_the_egress_policy_denies_and_selects_the_workload(
    server: Path, fleet_render: Render
) -> None:
    """Layer 4 of the no-egress posture: a default-deny NetworkPolicy, checked fleet-wide.

    Three clauses, each an independent way for "default-deny" to be false while looking unchanged:
    `Egress` absent from `policyTypes` (the direction is not governed); a non-empty `egress:` list
    (an explicit hole; an empty list, not an absent key, is what reads as deny-all); a `podSelector`
    that matches no pod (a label drift exempts the workload). Ports, ingress peers and scrape wiring
    are checked per server in `tests/test_deploy.py`.
    """
    spec = _network_policy(fleet_render, server)["spec"]
    assert isinstance(spec, dict)

    assert "Egress" in spec["policyTypes"], (
        f"{server.name}'s NetworkPolicy does not govern Egress, so outbound traffic is "
        "unrestricted while the file still looks like a default-deny policy"
    )
    assert spec["egress"] == [], (
        f"{server.name} may reach nothing at request time; its policy permits {spec['egress']!r}"
    )

    selector = spec["podSelector"]["matchLabels"]["app.kubernetes.io/name"]
    pod_label = _deployment(fleet_render, server)["spec"]["template"]["metadata"]["labels"][
        "app.kubernetes.io/name"
    ]
    assert selector == pod_label, (
        f"{server.name}'s NetworkPolicy selects {selector!r} and its Deployment labels the pod "
        f"{pod_label!r}: the policy binds to no workload, which denies nothing"
    )


#: The namespaces a Prometheus in this family scrapes from: `monitoring` (kube-prometheus-stack)
#: and `openshift-user-workload-monitoring` (OpenShift's user-workload Prometheus). Written as
#: literals, since deriving them from the checked objects would agree with whatever they hold.
SCRAPER_NAMESPACES = frozenset({"monitoring", "openshift-user-workload-monitoring"})


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_the_scrape_hole_admits_the_namespace_this_platform_runs_prometheus_in(
    server: Path, fleet_render: Render
) -> None:
    """The scrape ingress hole admits the namespaces the platform runs Prometheus in, and no others.

    On OpenShift the user-workload Prometheus is not in `monitoring`, so admitting only that would
    silently drop every scrape, including the fleet's egress and degradation counters. Held
    fleet-wide so a new server is bound; both directions, since admitting every namespace would also
    pass. Based on `NetworkPolicyPeer`'s documented semantics, not observed against an API server.
    """
    ingress = _network_policy(fleet_render, server)["spec"]["ingress"]
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


def _scaled_object(render: Render, server: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """The ScaledObject and its TriggerAuthentication, rendered under `autoscaling.mode=keda`."""
    scaled = _object(render, server, "ScaledObject", *KEDA)
    docs = [
        doc
        for doc in render(*KEDA)
        if doc["kind"] == "TriggerAuthentication"
        and doc["metadata"]["name"] == f"{_service_name(server)}-prometheus"
    ]
    assert len(docs) == 1, f"{server.name}: {len(docs)} TriggerAuthentication objects, expected one"
    return scaled, docs[0]


@pytest.mark.parametrize("server", gated_server_dirs(), ids=lambda path: path.name)
def test_the_keda_alternative_is_the_hpa_with_a_better_signal(
    server: Path, fleet_render: Render
) -> None:
    """Swapping the CPU HPA for the KEDA ScaledObject changes the signal and nothing else.

    Same target, floor, ceiling, behaviour and CPU trigger, so adopting KEDA moves no bound held
    here; the first trigger must read *this* server's admission occupancy, not a neighbour's.
    """
    scaled, auth = _scaled_object(fleet_render, server)
    hpa = _object(fleet_render, server, "HorizontalPodAutoscaler")["spec"]
    spec = scaled["spec"]
    assert spec["scaleTargetRef"]["name"] == _deployment(fleet_render, server)["metadata"]["name"]
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


def test_the_keda_alternative_is_not_applied_with_the_rest_of_deploy(fleet_render: Render) -> None:
    """Two autoscalers on one Deployment fight, so exactly one of them is ever rendered.

    By default no ScaledObject exists at all; under `autoscaling.mode=keda` a server gets the
    ScaledObject if and only if it has an admission gate, and then it has no HPA. Both directions,
    since a render that kept every HPA would also pass a check for the ScaledObjects alone.
    """
    gated = {path.name for path in gated_server_dirs()}
    for mode, args in (("hpa", ()), ("keda", KEDA)):
        docs = fleet_render(*args)
        scaled = {
            doc["spec"]["scaleTargetRef"]["name"] for doc in docs if doc["kind"] == "ScaledObject"
        }
        hpas = {
            doc["spec"]["scaleTargetRef"]["name"]
            for doc in docs
            if doc["kind"] == "HorizontalPodAutoscaler"
        }
        deployments = {doc["metadata"]["name"] for doc in docs if doc["kind"] == "Deployment"}
        assert not scaled & hpas, f"mode {mode}: {sorted(scaled & hpas)} have two autoscalers"
        assert scaled | hpas == deployments, (
            f"mode {mode}: {sorted(deployments - scaled - hpas)} have no autoscaler"
        )
        expected = {f"chemclaw-mcp-{name}" for name in gated} if mode == "keda" else set()
        assert scaled == expected, f"mode {mode}: ScaledObjects for {sorted(scaled)}"


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
def test_the_bearer_is_wired_from_the_secret_the_manifest_names(
    server: Path, fleet_render: Render
) -> None:
    """The Deployment hands the server exactly its manifest's `token_env`, from `chemclaw-secrets`.

    The server fails closed, so a missing bearer is a 401 on every `/mcp` call with `/healthz` at
    200. The name comes from the manifest, which both sides read. Not `optional`: a missing key
    should stop the pod starting, loudly.
    """
    token_env = _manifest_token_env(server)
    from_secrets = {
        entry["name"]: entry["valueFrom"]["secretKeyRef"]
        for entry in _pod_spec(fleet_render, server)["containers"][0].get("env", [])
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
def test_the_pod_runs_under_whatever_uid_the_platform_assigns(
    server: Path, fleet_render: Render
) -> None:
    """No pinned UID, GID or fsGroup; non-root still enforced; the image verifiable as non-root.

    OpenShift's `restricted-v2` SCC assigns a UID from the namespace range and rejects pinned ones.
    `runAsNonRoot` needs a numeric image `USER` to verify, and an assigned UID has no passwd entry,
    so `HOME` must point at a writable mounted path.
    """
    spec = _pod_spec(fleet_render, server)
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
def test_the_image_is_the_one_placeholder_the_overlay_rewrites(
    server: Path, fleet_render: Render
) -> None:
    """Every Deployment renders `registry.invalid/chemclaw-mcp-<name>:unset`, and nothing else.

    A release names its registry and a per-server digest in the chart's values
    (`docs/operations.md` §2); until it does, `.invalid` never resolves (RFC 2606), so an install
    without them fails with `ErrImagePull` instead of pulling whatever a third party registered
    under a short name on a public registry.
    """
    image = _pod_spec(fleet_render, server)["containers"][0]["image"]
    expected = f"registry.invalid/chemclaw-mcp-{server.name}:unset"
    assert image == expected, f"{server.name}: {image!r}"


def test_a_digest_pins_the_image_and_a_moving_tag_is_refused(fleet_render: Render) -> None:
    """The two ways a release names its bytes: a digest, or a fixed tag; never `latest`.

    A tag is a pointer, and a deployment that follows one cannot be rolled back to known bytes.
    The digest form is how a release descriptor pins an image; `latest` and an empty tag fail the
    render rather than reaching a cluster.
    """
    digest = "sha256:" + "ab" * 32
    docs = fleet_render(
        "--set",
        "defaults.image.registry=registry.example",
        "--set",
        f"servers.calc.image.digest={digest}",
    )
    images = {
        doc["metadata"]["name"]: doc["spec"]["template"]["spec"]["containers"][0]["image"]
        for doc in docs
        if doc["kind"] == "Deployment"
    }
    assert images["chemclaw-mcp-calc"] == f"registry.example/chemclaw-mcp-calc@{digest}"
    assert images["chemclaw-mcp-chem"] == "registry.example/chemclaw-mcp-chem:unset"
    for refused in ("latest", ""):
        with pytest.raises(pytest.fail.Exception, match="non-latest tag or a digest"):
            fleet_render("--set", f"defaults.image.tag={refused}")


# --- What a pod may ask of the node, and what values may do to a manifest ------------------------

#: Pod and container fields that hand a workload the host or privilege. None is a value the chart
#: has, so none may appear in any render; a template that learns one needs a decision, not a diff.
HOST_AND_PRIVILEGE_KEYS = frozenset(
    {
        "hostNetwork",
        "hostPID",
        "hostIPC",
        "hostPath",
        "hostPort",
        "hostUsers",
        "privileged",
        "shareProcessNamespace",
        "envFrom",
        "command",
        "args",
        "initContainers",
        "ephemeralContainers",
    }
)

ALL_MODES = (("hpa", ()), ("keda", KEDA), ("none", ("--set", "autoscaling.mode=none")))


def _keys(node: object) -> set[str]:
    """Every mapping key anywhere under `node`."""
    if isinstance(node, dict):
        return set(node) | {key for child in node.values() for key in _keys(child)}
    if isinstance(node, list):
        return {key for child in node for key in _keys(child)}
    return set()


def _shape(docs: list[dict[str, Any]]) -> set[tuple[str, ...]]:
    """The key paths of every object (kind, name, then keys; lists collapsed), values ignored.

    Two renders with the same shape differ only in scalar values. Injected YAML adds keys, so it is
    a different shape.
    """
    found: set[tuple[str, ...]] = set()

    def walk(node: object, path: tuple[str, ...]) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                found.add((*path, str(key)))
                walk(child, (*path, str(key)))
        elif isinstance(node, list):
            for child in node:
                walk(child, (*path, "[]"))

    for doc in docs:
        walk(doc, (doc["kind"], doc["metadata"]["name"]))
    return found


@pytest.mark.parametrize("mode", [name for name, _ in ALL_MODES])
def test_no_pod_asks_for_the_host_or_for_privilege(mode: str, fleet_render: Render) -> None:
    """No object in any autoscaling mode carries a host-access, privilege or command field.

    The default-deny NetworkPolicy does not govern `hostNetwork`, and `privileged` undoes the
    security context, so each is a way to void the posture while the checks on the fields that
    *are* there stay green.
    """
    args = dict(ALL_MODES)[mode]
    offences = sorted(HOST_AND_PRIVILEGE_KEYS & _keys(fleet_render(*args)))
    assert not offences, f"the render carries {offences}"


#: The only servers whose root filesystem is writable: `calc` runs xtb and CREST, `pyexec` runs
#: caller programs. Written as literals, so a server that opens its root filesystem has to say so
#: here, in review, rather than by setting a value.
WRITABLE_ROOT_SERVERS = frozenset({"calc", "pyexec"})


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_the_root_filesystem_is_read_only_except_where_the_server_writes(
    server: Path, fleet_render: Render
) -> None:
    """`readOnlyRootFilesystem` is true for every server not named in `WRITABLE_ROOT_SERVERS`."""
    container = _pod_spec(fleet_render, server)["containers"][0]
    read_only = container["securityContext"]["readOnlyRootFilesystem"]
    assert read_only is (server.name not in WRITABLE_ROOT_SERVERS), (
        f"{server.name}: readOnlyRootFilesystem={read_only!r}, and "
        f"{sorted(WRITABLE_ROOT_SERVERS)} are the servers allowed to write outside /tmp"
    )


#: The most scratch a server may be given. A value above its ceiling is a decision about node disk,
#: so it is made here rather than in a values file. `calc` writes xtb and CREST output for runs of
#: minutes to hours, `pyexec` is the one caller-controlled writer, and the two model servers stage
#: weights; everything else is `tempfile` and nothing.
DEFAULT_SCRATCH_CEILING = "64Mi"
SCRATCH_CEILINGS = {
    "calc": "4Gi",
    "pyexec": "256Mi",
    "rxnlabel": "256Mi",
    "rxnpredict": "256Mi",
}
_BINARY = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}


def _bytes(quantity: str) -> int:
    """A binary-suffixed Kubernetes quantity in bytes."""
    return int(quantity[:-2]) * _BINARY[quantity[-2:]]


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_scratch_stays_under_the_ceiling_argued_for_the_server(
    server: Path, fleet_render: Render
) -> None:
    """The `/tmp` emptyDir's `sizeLimit` is at most this server's ceiling."""
    limit = _pod_spec(fleet_render, server)["volumes"][0]["emptyDir"]["sizeLimit"]
    ceiling = SCRATCH_CEILINGS.get(server.name, DEFAULT_SCRATCH_CEILING)
    assert _bytes(limit) <= _bytes(ceiling), (
        f"{server.name}: scratch {limit} is above its ceiling {ceiling}; a bigger emptyDir is a "
        "bigger share of node disk for one pod to fill, so raise the ceiling here, with the reason"
    )


@pytest.mark.parametrize("server", gated_server_dirs(), ids=lambda path: path.name)
def test_the_keda_trigger_is_the_one_argued_for(server: Path, fleet_render: Render) -> None:
    """The occupancy trigger's constants, as literals: 0.7 per pod, an empty series reads as idle.

    `docs/autoscaling.md` argues for each. A threshold of 0.1 scales the fleet out on a single
    call, and `ignoreNullValues: "false"` turns a fresh install's missing series into an error that
    blocks the CPU trigger too.
    """
    scaled, _ = _scaled_object(fleet_render, server)
    spec = scaled["spec"]
    prometheus = spec["triggers"][0]
    assert prometheus["metricType"] == "AverageValue"
    assert prometheus["metadata"]["threshold"] == "0.7"
    assert prometheus["metadata"]["ignoreNullValues"] == "true"
    assert prometheus["metadata"]["authModes"] == "bearer"
    assert spec["pollingInterval"] == 15
    assert spec["triggers"][1]["metricType"] == "Utilization"


# --- Values must not be able to write manifest ----------------------------------------------

INJECTION = (
    "\nhostNetwork: true\nhostPID: true\nprivileged: true\nenvFrom: [{secretRef: {name: x}}]\n"
)

#: What helm prints when `values.schema.json` refuses a values file.
SCHEMA_REFUSAL = r"values don't meet the specifications of the schema"

#: Every string a template interpolates, with the helm arguments that reach it. The KEDA ones need
#: the mode that renders them.
HOSTILE_FIELDS = [
    ("servers.props.scratch", ()),
    ("defaults.scratch", ()),
    ("servers.props.image.tag", ()),
    ("servers.props.image.digest", ()),
    ("defaults.image.registry", ()),
    ("defaults.image.namePrefix", ()),
    ("defaults.image.pullPolicy", ()),
    ("secretName", ()),
    ("serviceMonitor.interval", ()),
    ("serviceMonitor.scrapeTimeout", ()),
    ("networkPolicy.callerLabels.app\\.kubernetes\\.io/name", ()),
    ("networkPolicy.scraperNamespaces[0]", ()),
    ("servers.props.resources.requests.cpu", ()),
    ("servers.props.resources.limits.memory", ()),
    ("autoscaling.keda.serverAddress", KEDA),
    ("autoscaling.keda.namespace", KEDA),
    ("autoscaling.keda.threshold", KEDA),
]


def _hostile(field: str, extra: tuple[str, ...]) -> tuple[str, ...]:
    """Helm arguments setting `field` to a string that carries `INJECTION`."""
    return (*extra, "--set-string", f"{field}=x{INJECTION}")


@pytest.mark.parametrize(("field", "extra"), HOSTILE_FIELDS, ids=[f for f, _ in HOSTILE_FIELDS])
def test_the_schema_refuses_a_value_that_carries_yaml(
    field: str, extra: tuple[str, ...], fleet_render: Render
) -> None:
    """With the schema in place, a newline-bearing value in any interpolated field is refused."""
    with pytest.raises(pytest.fail.Exception, match=SCHEMA_REFUSAL):
        fleet_render(*_hostile(field, extra))


@pytest.mark.parametrize(("field", "extra"), HOSTILE_FIELDS, ids=[f for f, _ in HOSTILE_FIELDS])
def test_the_templates_quote_what_the_schema_would_have_refused(
    field: str, extra: tuple[str, ...], fleet_render: Render, fleet_render_without_schema: Render
) -> None:
    """With the schema removed, the same values render as strings and add no key to any object.

    The second layer: a pattern that is wrong or skipped must not be what stands between a values
    file and `hostNetwork`. The render has the same key paths as the default render in the same
    mode, and none of the host or privilege fields.
    """
    docs = fleet_render_without_schema(*_hostile(field, extra))
    baseline = fleet_render(*extra) if extra == KEDA else fleet_render()
    assert _shape(docs) == _shape(baseline), (
        f"{field}: the render gained {sorted(_shape(docs) - _shape(baseline))[:3]}"
    )
    assert not HOST_AND_PRIVILEGE_KEYS & _keys(docs)


@pytest.mark.parametrize(
    "refused",
    [
        ("serviceMonitor.enable", "true"),
        ("autoscaling.keda.typo", "1"),
        ("autoscaling.modee", "hpa"),
        ("servers.props.resources.foo", "1"),
        ("servers.props.resources.requests.gpu", "1"),
        ("defaults.hostNetwork", "true"),
        ("servers.props.hostNetwork", "true"),
        ("networkPolicy.egressPeers", "x"),
    ],
    ids=lambda pair: pair[0],
)
def test_an_unknown_key_is_refused_rather_than_ignored(
    refused: tuple[str, str], fleet_render: Render
) -> None:
    """A typo in a values file is an error. Every object in the schema is closed."""
    with pytest.raises(pytest.fail.Exception, match="Additional property"):
        fleet_render("--set", f"{refused[0]}={refused[1]}")


@pytest.mark.parametrize(
    "refused",
    [
        "servers.props.resources.requests.cpu=lots",
        "servers.props.resources.limits.memory=4GB",
        "servers.props.scratch=64",
        "serviceMonitor.interval=30",
        "autoscaling.keda.serverAddress=ftp://thanos",
        "servers.props.image.pullPolicy=Sometimes",
    ],
)
def test_a_quantity_or_address_of_the_wrong_shape_is_refused(
    refused: str, fleet_render: Render
) -> None:
    """The typed fields have patterns, so a unit that Kubernetes would reject fails at render."""
    with pytest.raises(pytest.fail.Exception, match=SCHEMA_REFUSAL):
        fleet_render("--set-string", refused)


def test_two_servers_cannot_claim_one_port_and_a_ceiling_cannot_undercut_its_floor(
    fleet_render: Render,
) -> None:
    """Rules across fields, which a per-field schema cannot state, fail the render by name."""
    with pytest.raises(pytest.fail.Exception, match="both claim port 8851"):
        fleet_render("--set", "servers.props.port=8851")
    with pytest.raises(pytest.fail.Exception, match="maxReplicas 2 is below replicas 3"):
        fleet_render("--set", "servers.calc.replicas=3", "--set", "servers.calc.maxReplicas=2")


def test_a_values_file_without_a_digest_renders_an_image_that_cannot_run(
    fleet_render: Render,
) -> None:
    """Naming a registry but no digest still renders `:unset`; installing then fails at the pull.

    Intended, and the guard against deploying a fleet nobody pinned: `docs/operations.md` §2 says a
    release names a digest for every server. With no registry either, the host is `.invalid`
    (RFC 2606), which can never resolve. A tag that exists is the other way to name bytes.
    """
    for args in ((), ("--set", "defaults.image.registry=registry.example")):
        for doc in fleet_render(*args):
            if doc["kind"] != "Deployment":
                continue
            image = doc["spec"]["template"]["spec"]["containers"][0]["image"]
            assert image.endswith(":unset"), f"{doc['metadata']['name']}: {image}"
            if not args:
                assert image.startswith("registry.invalid/"), image


# --- The cost table in MODULES.md is the chart's, not a second copy ------------------------------

_COST_ROW = re.compile(
    r"^\| `(?P<name>[a-z0-9]+)` \| (?P<low>\d+) → (?P<high>\d+) \| (?P<req>[^|]+?) \| "
    r"(?P<base>[^|]+?) \| (?P<ceil>[^|]+?) \|$",
    re.M,
)


def _cores(quantity: str) -> float:
    """CPU cores in a Kubernetes quantity (`250m`, `1`)."""
    return float(quantity[:-1]) / 1000 if quantity.endswith("m") else float(quantity)


def _gibibytes(quantity: str) -> float:
    """Memory in GiB for a `Mi` or `Gi` quantity."""
    return int(quantity[:-2]) / 1024 if quantity.endswith("Mi") else float(int(quantity[:-2]))


def test_the_cost_table_in_modules_md_is_what_the_chart_renders(fleet_render: Render) -> None:
    """Replicas, requests, baseline and ceiling per server, and the fleet totals, are the chart's.

    The table is prose a person copied out of the values; this recomputes every cell from the
    render, so a changed request or ceiling that does not move the table is red.
    """
    catalogue = (ROOT / "MODULES.md").read_text(encoding="utf-8")
    section = catalogue.split("## What the built fleet costs to run", 1)[1].split("\n## ", 1)[0]
    rows = {match["name"]: match for match in _COST_ROW.finditer(section)}
    totals = {"base_cpu": 0.0, "base_mem": 0.0, "ceil_cpu": 0.0, "ceil_mem": 0.0}
    for server in server_dirs():
        low = _object(fleet_render, server, "HorizontalPodAutoscaler")["spec"]["minReplicas"]
        high = _object(fleet_render, server, "HorizontalPodAutoscaler")["spec"]["maxReplicas"]
        requests = _pod_spec(fleet_render, server)["containers"][0]["resources"]["requests"]
        cpu, memory = str(requests["cpu"]), str(requests["memory"])
        row = rows.get(server.name)
        assert row, f"MODULES.md's cost table has no row for {server.name}"
        base_cpu, base_mem = low * _cores(cpu), low * _gibibytes(memory)
        ceil_cpu, ceil_mem = high * _cores(cpu), high * _gibibytes(memory)
        expected = (
            f"{low} → {high}",
            f"{cpu} / {memory}",
            f"{base_cpu:g} CPU, {base_mem:g} Gi",
            f"{ceil_cpu:g} CPU, {ceil_mem:g} Gi",
        )
        found = (f"{row['low']} → {row['high']}", row["req"], row["base"], row["ceil"])
        assert found == expected, (
            f"MODULES.md row {server.name}: {found} but the chart gives {expected}"
        )
        totals["base_cpu"] += base_cpu
        totals["base_mem"] += base_mem
        totals["ceil_cpu"] += ceil_cpu
        totals["ceil_mem"] += ceil_mem
    assert set(rows) == {path.name for path in server_dirs()}, "the cost table lists other servers"
    assert (
        f"**{totals['base_cpu']:g} CPU, {totals['base_mem']:g} Gi**" in section
        and f"**{totals['ceil_cpu']:g} CPU, {totals['ceil_mem']:g} Gi**" in section
    ), f"MODULES.md's fleet row is not the sum of the rows above it: {totals}"
