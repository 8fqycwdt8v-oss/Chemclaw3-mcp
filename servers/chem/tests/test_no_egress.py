"""This server's own code holds no way to call out.

The scan covers the whole package. Loopback named in `app.py`'s docstring is exempt by design.
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
    """The names this server resolves are on disk, checksummed, and licensed.

    An external resolver (PubChem, OPSIN) is the obvious alternative and forbidden; the suite runs
    with the guard armed, which proves the table suffices.
    """
    from chemclaw_mcp_chem.engine.reagents import dataset, resolve_compound_name

    loaded = dataset()
    assert loaded.records_path.is_file()
    assert loaded.records_path.is_relative_to(PACKAGE)
    assert loaded.licence and loaded.retrieved_from
    assert resolve_compound_name("Pd(dppf)Cl2") is not None
