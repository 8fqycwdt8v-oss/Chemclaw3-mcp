"""Whether a capability in this fleet can survive a rollout, and whether it has a capacity lever.

Each server's own `tests/test_deploy.py` checks the things a server can see about itself — its port,
its labels, its securityContext, its egress rule. None of them could see the property that mattered
most at 200 users, because it was identical in all seven and therefore invisible in each: **every
server shipped `replicas: 1` with no autoscaler, no disruption budget, no topology spread and no
`terminationGracePeriodSeconds`.** So a node drain, an image rollout, an eviction or one failed
liveness probe was a 100% outage of that capability for every user, and there was no second pod to
add when the first one filled.

Measured before this file existed, on `servers/calc` — the pod every process-chemistry turn reaches:
4 admission slots, 1 replica, and 60 concurrent `compute_xtb_energy` calls refused **68%** of the
time, with one `optimize_geometry` on a 50-atom fragment costing 390 core-seconds by itself. There
was no knob that added capacity; `CHEMCLAW_CALC_MAX_CONCURRENT_REQUESTS`, which the refusal message
suggests raising, does not add cores.

**The derivation is the point of the grace-period check, not the number.** A server's
`connector.yaml` already declares `request_timeout` — the longest a caller waits — which is also the
longest an in-flight call is still worth finishing, because this fleet is stateless by design and a
result exists nowhere until the response is written. So the grace period is that number plus a fixed
drain, checked here rather than transcribed into seven files that then drift apart. Chemclaw3
derives its own front door's grace period from its turn budget the same way and for the same reason;
this fleet is where that fix never arrived.

Everything here reads the server list off the filesystem, never a list in this file: a server added
next year is covered the day its directory exists, which is the failure mode a hand-kept list has.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

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

    Read out of the text rather than the parsed document on purpose: this is the one number the
    grace period is derived from, so a manifest that stopped declaring it should fail here with a
    message naming the file, not with a `KeyError` several frames away.
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

    Both directions. Too short and Kubernetes SIGKILLs work the caller is still waiting for — the
    shipped default of 30 s killed a `calc` optimisation 30 s into 390 s, and this fleet writes
    nothing down until a call returns, so that work is simply lost. Too long and every rollout and
    every node drain stalls on a pod nobody is waiting for any more.
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
    """An unbounded `emptyDir` draws on the *node's* disk, so one pod evicts its neighbours.

    `calc` writes xtb and CREST scratch here for runs of minutes to hours, and `pyexec` lets
    caller-supplied Python write up to `Limits.file_bytes` per call. Filling the node is a
    `DiskPressure` eviction that takes unrelated pods with it — the one failure a fleet of small
    single-purpose pods cannot absorb. With a `sizeLimit` the kubelet evicts the offending pod
    alone.
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

    The floor check is the one that catches a real drift: an HPA whose `minReplicas` is below the
    Deployment's `replicas` silently scales the baseline *down* on the first quiet minute, which
    undoes `test_a_capability_is_never_one_pod` without touching the file it asserts.
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
    """Utilization is measured against `requests.cpu`, so an under-set request breaks the HPA.

    This is the half of a CPU-based autoscaler that is easy to ship wrong and impossible to see: at
    `requests.cpu: 100m` — which five of these servers shipped — one caller doing a single core's
    work reads as 1000% utilization, and the autoscaler runs to `maxReplicas` on one request. The
    requests in `deployment.yaml` are set to the draw of a pod doing real work for exactly this
    reason, so the check is that they are not the token value.
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
