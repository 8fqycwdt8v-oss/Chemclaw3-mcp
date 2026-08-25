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
