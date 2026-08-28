"""The deployment says what the code says — and for this server it says the most important part.

Every other server's NetworkPolicy is the layer that holds if the Python is wrong. This server's is
the layer that holds when the in-process guards are treated as what they are: defence in depth
around an interpreter running code a language model wrote. So this file is written in both
directions, because the realistic regression is not somebody adding an egress rule on purpose — it
is somebody dropping `Egress` from `policyTypes` while debugging and leaving the file looking
unchanged.
"""

from __future__ import annotations

from pathlib import Path

import yaml

POLICY = Path(__file__).resolve().parents[1] / "deploy" / "networkpolicy.yaml"


def _policy() -> dict[str, object]:
    """The parsed NetworkPolicy."""
    loaded = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_egress_is_denied() -> None:
    """`Egress` is governed and the rule list is empty — the two halves of "deny all"."""
    spec = _policy()["spec"]
    assert isinstance(spec, dict)
    assert "Egress" in spec["policyTypes"], (
        "dropping Egress from policyTypes restores unrestricted outbound traffic while leaving "
        "this file looking like a network policy — and this is the server where that matters most"
    )
    assert spec["egress"] == [], f"pyexec must reach nothing; found {spec['egress']!r}"


def test_ingress_admits_only_the_agent_and_the_scraper() -> None:
    """An interpreter open to the whole namespace is an interpreter open to the whole namespace."""
    spec = _policy()["spec"]
    assert isinstance(spec, dict)
    ingress = spec["ingress"]
    assert isinstance(ingress, list)
    assert len(ingress) == 1
    sources = ingress[0]["from"]
    selectors = [next(iter(entry)) for entry in sources]
    assert selectors == ["podSelector", "namespaceSelector"]
    assert ingress[0]["ports"] == [{"protocol": "TCP", "port": 8899}]


def test_the_scrape_is_wired_end_to_end() -> None:
    """`/metrics` is only observability if something is told to collect it, and nothing was.

    Every NetworkPolicy in this fleet admits the monitoring namespace on the server's port — the
    hole has been open since the first server shipped — and `deploy/` held exactly one file, that
    policy. No Service, no ServiceMonitor, no PodMonitor: Prometheus had no way to discover a
    single pod here, so every counter this repository emits would have gone nowhere.

    The chain this asserts is four links, and the weakest is the last. A ServiceMonitor's
    `endpoints[].port` is a **port name**, resolved through the Service, and a name that matches
    nothing produces no targets and **no error** — the failure a scrape configuration has when
    nobody checks it is silence, which is indistinguishable from a healthy server nobody is
    calling. So the number is held against the Containerfile and the manifest by the test above,
    and the *name* is held between these two files here.
    """
    deploy = POLICY.parent
    service = yaml.safe_load((deploy / "service.yaml").read_text(encoding="utf-8"))
    monitor = yaml.safe_load((deploy / "servicemonitor.yaml").read_text(encoding="utf-8"))
    assert service["spec"]["selector"]["app.kubernetes.io/name"] == "chemclaw-mcp-pyexec"
    ports = service["spec"]["ports"]
    assert ports == [{"name": "http", "protocol": "TCP", "port": 8899, "targetPort": 8899}]
    assert monitor["spec"]["selector"]["matchLabels"]["app.kubernetes.io/name"] == (
        "chemclaw-mcp-pyexec"
    )
    endpoints = monitor["spec"]["endpoints"]
    assert len(endpoints) == 1, "one process, one port, one scrape"
    assert endpoints[0]["port"] == ports[0]["name"], (
        "the ServiceMonitor names a port the Service does not declare; Prometheus resolves that "
        "name through the Service and reports no targets and no error when it misses"
    )
    assert endpoints[0]["path"] == "/metrics"


# --- Pod hardening (added with the securityContext Deployments) -------------------------------
DEPLOYMENT = POLICY.parent / "deployment.yaml"


def _deployment() -> dict[str, object]:
    """The parsed Deployment."""
    loaded = yaml.safe_load(DEPLOYMENT.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_the_pod_is_hardened() -> None:
    """Every deny-by-default securityContext field is present and set — the fields a plain cluster
    otherwise leaves at root/all-capabilities/unconfined/unbounded, since `deploy/` shipped no
    workload at all until these files were added.
    """
    dep = _deployment()
    spec = dep["spec"]["template"]["spec"]  # type: ignore[index]
    assert spec["automountServiceAccountToken"] is False
    pod_sc = spec["securityContext"]
    assert pod_sc["runAsNonRoot"] is True
    assert pod_sc["seccompProfile"]["type"] == "RuntimeDefault"
    assert int(pod_sc["runAsUser"]) >= 1000

    container = spec["containers"][0]
    csc = container["securityContext"]
    assert csc["allowPrivilegeEscalation"] is False
    assert csc["runAsNonRoot"] is True
    assert csc["capabilities"]["drop"] == ["ALL"]
    assert csc["seccompProfile"]["type"] == "RuntimeDefault"
    # Present and a boolean either way: true for a server that writes nothing, false for one that
    # needs scratch (`calc`, `pyexec`).
    assert isinstance(csc["readOnlyRootFilesystem"], bool)

    resources = container["resources"]
    assert resources["requests"]["cpu"] and resources["requests"]["memory"]
    assert resources["limits"]["cpu"] and resources["limits"]["memory"]

    for probe in ("readinessProbe", "livenessProbe"):
        assert container[probe]["httpGet"]["path"] == "/healthz"
        assert container[probe]["httpGet"]["port"] == "http"


def test_the_pod_label_matches_the_networkpolicy_selector() -> None:
    """The highest-value check here: a one-character drift between the Deployment's pod label and
    the NetworkPolicy's `podSelector` silently exempts this workload from default-deny egress, and
    the no-egress promise is void for it with nothing red. So the two are checked against each
    other, not against a literal.
    """
    dep = _deployment()
    pod_label = dep["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/name"]  # type: ignore[index]
    policy = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
    selector = policy["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/name"]
    assert pod_label == selector, (
        f"Deployment pod label {pod_label!r} != NetworkPolicy podSelector {selector!r}: "
        "the egress policy would not select this workload"
    )
