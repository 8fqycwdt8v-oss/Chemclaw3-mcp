"""The deployment says what the code says, asserted in both directions.

Sharper here than for `props`: this server's predictors are ML libraries that fetch weights when
they can, so the empty `egress:` block is the layer that makes "the weights are baked in" a fact
rather than an intention.
"""

from __future__ import annotations

from pathlib import Path

import yaml

POLICY = Path(__file__).resolve().parents[1] / "deploy" / "networkpolicy.yaml"


def _spec() -> dict[str, object]:
    """The parsed NetworkPolicy spec."""
    loaded = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    spec = loaded["spec"]
    assert isinstance(spec, dict)
    return spec


def test_egress_is_denied() -> None:
    """`Egress` governed and the rule list empty — the two halves of "deny all"."""
    spec = _spec()
    assert "Egress" in spec["policyTypes"]
    assert spec["egress"] == [], f"rxnpredict must reach nothing; found {spec['egress']!r}"


def test_ingress_admits_only_the_agent_and_the_scraper() -> None:
    """A prediction surface open to the namespace is a prediction surface open to the namespace."""
    spec = _spec()
    assert "Ingress" in spec["policyTypes"]
    rules = spec["ingress"]
    assert isinstance(rules, list) and len(rules) == 1
    assert rules[0]["ports"] == [{"protocol": "TCP", "port": 8857}]


def test_the_policy_selects_this_server() -> None:
    """A selector that matches nothing protects nothing."""
    assert (
        _spec()["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "chemclaw-mcp-rxnpredict"
    )


def test_the_image_runs_offline_and_on_the_declared_port() -> None:
    """The offline env vars are what stop transformers reaching out on first load."""
    containerfile = (POLICY.parents[1] / "Containerfile").read_text(encoding="utf-8")
    assert "HF_HUB_OFFLINE=1" in containerfile
    assert "TRANSFORMERS_OFFLINE=1" in containerfile
    assert "8857" in containerfile
    manifest = yaml.safe_load((POLICY.parents[1] / "connector.yaml").read_text(encoding="utf-8"))
    assert ":8857/mcp" in manifest["endpoint"]["url"]


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
    assert service["spec"]["selector"]["app.kubernetes.io/name"] == "chemclaw-mcp-rxnpredict"
    ports = service["spec"]["ports"]
    assert ports == [{"name": "http", "protocol": "TCP", "port": 8857, "targetPort": 8857}]
    assert monitor["spec"]["selector"]["matchLabels"]["app.kubernetes.io/name"] == (
        "chemclaw-mcp-rxnpredict"
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
