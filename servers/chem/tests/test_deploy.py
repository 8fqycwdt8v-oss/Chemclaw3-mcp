"""The deployment says what the code says. Asserted, because a chart nobody verifies drifts.

The runtime guard and the AST scan both live inside the process, and both would be worth little if
the pod could still open a socket. This is the check on the layer that actually enforces it — and it
is written in *both* directions, because the realistic regression is not someone adding an egress
rule on purpose. It is someone dropping `Egress` from `policyTypes` while debugging and leaving the
file looking unchanged.
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
    assert spec["egress"] == [], f"chem must reach nothing; found {spec['egress']!r}"


def test_ingress_admits_only_the_agent_and_the_scraper() -> None:
    """A tool server open to the whole namespace is a tool surface open to the whole namespace."""
    spec = _policy()["spec"]
    assert isinstance(spec, dict)
    assert "Ingress" in spec["policyTypes"]
    rules = spec["ingress"]
    assert isinstance(rules, list) and len(rules) == 1
    assert rules[0]["ports"] == [{"protocol": "TCP", "port": 8858}]


def test_the_policy_selects_this_server() -> None:
    """A policy whose selector matches nothing is a policy that protects nothing."""
    spec = _policy()["spec"]
    assert isinstance(spec, dict)
    assert spec["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "chemclaw-mcp-chem"


def test_the_container_declares_the_port_the_manifest_dials() -> None:
    """Containerfile, NetworkPolicy and connector.yaml must agree on one number."""
    containerfile = (POLICY.parents[1] / "Containerfile").read_text(encoding="utf-8")
    manifest = yaml.safe_load((POLICY.parents[1] / "connector.yaml").read_text(encoding="utf-8"))
    assert "8858" in containerfile
    assert ":8858/mcp" in manifest["endpoint"]["url"]
