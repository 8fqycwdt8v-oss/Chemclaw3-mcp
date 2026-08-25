"""This server's own code holds no way to call out. Three lines, and every server ships them.

The scan covers the whole package — engine, tools and transport — because the rule is about the
server, not about one layer of it. `app.py` names loopback in its docstring, which the scanner
exempts on purpose: showing somebody how to reach the server they are running is documentation,
while naming somebody else's host is the thing being forbidden.
"""

from __future__ import annotations

from pathlib import Path

import chemclaw_mcp_rxnlabel
from mcp_server_kit.no_egress import assert_no_egress_sources

PACKAGE = Path(chemclaw_mcp_rxnlabel.__file__).parent


def test_no_module_can_reach_the_network() -> None:
    """No HTTP client imported, no remote host named — checked by AST, not by grep."""
    assert_no_egress_sources(PACKAGE)


def test_the_models_are_loaded_from_the_image_and_never_fetched() -> None:
    """The positive half, and here it is about *when* rather than about *where*.

    This server has no vendored corpus — its data is a SMARTS list in `species.py` and a solvent
    list in `agents.py`, both source. What it does have is model weights, and RXNMapper's library
    downloads its checkpoint on first use. The `Containerfile` bakes them at build time precisely
    so that never happens at request time, and the NetworkPolicy denies egress so it cannot happen
    even if the bake failed.

    What is asserted here is the code-level half of that: nothing in this package asks for a model
    by URL or triggers a download path of its own. The build-time bake is asserted by
    `tests/test_fleet.py` reading the `Containerfile`, and the runtime denial by
    `tests/test_deploy.py` reading the NetworkPolicy — three layers, checked in three places,
    because "no outbound call at request time" is this fleet's one unconditional rule.
    """
    from chemclaw_mcp_rxnlabel.engine import mapping, naming, version

    # Absent or present, the version says which — so a deployment whose bake silently failed is
    # visible in every label it produces rather than only in a log line nobody reads.
    components = version.components()
    assert (components["atom_mapper"] == "absent") is not mapping.available()
    assert (components["reaction_namer"] == "absent") is not naming.available()
