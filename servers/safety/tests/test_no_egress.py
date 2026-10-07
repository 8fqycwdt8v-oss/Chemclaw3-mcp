"""This server's own code holds no way to call out.

The scan covers the whole package. A loopback address in `app.py`'s docstring is exempt by
design: naming the server you run is documentation, naming somebody else's host is forbidden.
"""

from __future__ import annotations

from pathlib import Path

import chemclaw_mcp_safety
from mcp_server_kit.no_egress import assert_no_egress_sources

PACKAGE = Path(chemclaw_mcp_safety.__file__).parent


def test_no_module_can_reach_the_network() -> None:
    """No HTTP client imported, no remote host named — checked by AST, not by grep."""
    assert_no_egress_sources(PACKAGE)


def test_the_answers_come_from_the_vendored_corpora() -> None:
    """Every table this server answers from is on disk and checksummed.

    The tempting alternative is a live SDS or hazard service, which no-egress forbids; the vendored
    tables are proven sufficient because the suite runs with the guard armed. A fallback lookup that
    failed would otherwise report "no rule matched" for chemistry it never looked at.
    """
    from chemclaw_mcp_safety.engine import genotox, ich, screen

    for directory in (screen.RULES_DIR, genotox.ALERTS_DIR, ich.Q3C_DIR, ich.Q3D_DIR):
        assert directory.is_relative_to(PACKAGE)
        assert (directory / "dataset.json").is_file()
    assert screen.screen_structure("CCCN=[N+]=[N-]").flags
    assert genotox.screen_genotoxic_alerts(["CN(C)N=O"]).alerts
    assert ich.impurity_limit("Pd").limit is not None
