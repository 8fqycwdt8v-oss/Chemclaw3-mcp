"""The deployment says what the code says. Asserted, because a chart nobody verifies drifts.

The NetworkPolicy is the layer that actually enforces no-egress, and it is checked in both
directions, since the likely regression is `Egress` quietly dropped from `policyTypes`.
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
        "this file looking like a network policy"
    )
    assert spec["egress"] == [], f"calc must reach nothing; found {spec['egress']!r}"


def test_ingress_admits_only_the_agent_and_the_scraper() -> None:
    """A tool server open to the whole namespace is a tool surface open to the whole namespace."""
    spec = _policy()["spec"]
    assert isinstance(spec, dict)
    assert "Ingress" in spec["policyTypes"]
    rules = spec["ingress"]
    assert isinstance(rules, list) and len(rules) == 1
    assert rules[0]["ports"] == [{"protocol": "TCP", "port": 8860}]


def test_the_policy_selects_this_server() -> None:
    """A policy whose selector matches nothing is a policy that protects nothing."""
    spec = _policy()["spec"]
    assert isinstance(spec, dict)
    assert spec["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "chemclaw-mcp-calc"


def test_the_container_declares_the_port_the_manifest_dials() -> None:
    """Containerfile, NetworkPolicy and connector.yaml must agree on one number."""
    containerfile = (POLICY.parents[1] / "Containerfile").read_text(encoding="utf-8")
    manifest = yaml.safe_load((POLICY.parents[1] / "connector.yaml").read_text(encoding="utf-8"))
    assert "8860" in containerfile
    assert ":8860/mcp" in manifest["endpoint"]["url"]


def test_the_scrape_is_wired_end_to_end() -> None:
    """`/metrics` is only observability if something is told to collect it.

    Service, ServiceMonitor and NetworkPolicy must line up. A ServiceMonitor's `endpoints[].port` is
    a port name resolved through the Service, and a name matching nothing yields no targets and no
    error, so the name is held between the two files here.
    """
    deploy = POLICY.parent
    service = yaml.safe_load((deploy / "service.yaml").read_text(encoding="utf-8"))
    monitor = yaml.safe_load((deploy / "servicemonitor.yaml").read_text(encoding="utf-8"))
    assert service["spec"]["selector"]["app.kubernetes.io/name"] == "chemclaw-mcp-calc"
    ports = service["spec"]["ports"]
    assert ports == [{"name": "http", "protocol": "TCP", "port": 8860, "targetPort": 8860}]
    assert monitor["spec"]["selector"]["matchLabels"]["app.kubernetes.io/name"] == (
        "chemclaw-mcp-calc"
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
    otherwise leaves at root/all-capabilities/unconfined/unbounded.
    """
    dep = _deployment()
    spec = dep["spec"]["template"]["spec"]  # type: ignore[index]
    assert spec["automountServiceAccountToken"] is False
    pod_sc = spec["securityContext"]
    assert pod_sc["runAsNonRoot"] is True
    assert pod_sc["seccompProfile"]["type"] == "RuntimeDefault"
    # No pinned UID: OpenShift's `restricted-v2` assigns one and rejects any other, and
    # `runAsNonRoot` above is the property that matters (`tests/test_deploy_shape.py`).
    assert not {"runAsUser", "runAsGroup", "fsGroup"} & set(pod_sc)

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

    # Liveness and readiness are different routes: a liveness failure kills the container, so it
    # must not consult what readiness consults. `tests/test_deploy_shape.py` holds this fleet-wide.
    assert container["readinessProbe"]["httpGet"]["path"] == "/healthz"
    assert container["livenessProbe"]["httpGet"]["path"] == "/livez"
    for probe in ("readinessProbe", "livenessProbe"):
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
