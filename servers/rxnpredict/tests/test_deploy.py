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
