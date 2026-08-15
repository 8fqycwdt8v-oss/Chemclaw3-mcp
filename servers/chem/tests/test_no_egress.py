"""This server's own code holds no way to call out. Three lines, and every server ships them.

The scan covers the whole package — engine, tools and transport — because the rule is about the
server, not about one layer of it. `app.py` names loopback in its docstring, which the scanner
exempts on purpose: showing somebody how to reach the server they are running is documentation,
while naming somebody else's host is the thing being forbidden.
"""

from __future__ import annotations

from pathlib import Path

import chemclaw_mcp_chem
from mcp_server_kit.no_egress import assert_no_egress_sources

PACKAGE = Path(chemclaw_mcp_chem.__file__).parent


def test_no_module_can_reach_the_network() -> None:
    """No HTTP client imported, no remote host named — checked by AST, not by grep."""
    assert_no_egress_sources(PACKAGE)


def test_the_answers_come_from_the_vendored_corpus() -> None:
    """The positive half: the names this server resolves are on disk, checksummed, and licensed.

    Worth stating for this server in particular. Chemclaw3's own module argued the point at length —
    an external resolver (PubChem, OPSIN) is the obvious thing to reach for and is exactly what the
    no-egress rule forbids — so the table being sufficient is a property that has to be provable
    rather than assumed. It is provable because the whole suite runs with the guard armed.
    """
    from chemclaw_mcp_chem.engine.reagents import dataset, resolve_compound_name

    loaded = dataset()
    assert loaded.records_path.is_file()
    assert loaded.records_path.is_relative_to(PACKAGE)
    assert loaded.licence and loaded.retrieved_from
    assert resolve_compound_name("Pd(dppf)Cl2") is not None
