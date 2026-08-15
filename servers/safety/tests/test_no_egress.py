"""This server's own code holds no way to call out. Three lines, and every server ships them.

The scan covers the whole package — engine, tools and transport — because the rule is about the
server, not about one layer of it. `app.py` names loopback in its docstring, which the scanner
exempts on purpose: showing somebody how to reach the server they are running is documentation,
while naming somebody else's host is the thing being forbidden.
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
    """The positive half: every table this server answers from is on disk and checksummed.

    Worth stating for this server in particular. The obvious thing to reach for here is a live
    source — an SDS service, a hazard database, ICH's own site for the current revision — and a
    request-time call to any of them is what the no-egress rule forbids. That the four vendored
    tables are *sufficient* is therefore a property that has to be provable rather than assumed, and
    it is provable because the whole suite runs with the guard armed.

    The failure it rules out is specific and quiet: a screen that fell back to a network lookup and
    got a connection error would report "no rule matched" — this server's one forbidden answer — for
    chemistry it never looked at.
    """
    from chemclaw_mcp_safety.engine import genotox, ich, screen

    for directory in (screen.RULES_DIR, genotox.ALERTS_DIR, ich.Q3C_DIR, ich.Q3D_DIR):
        assert directory.is_relative_to(PACKAGE)
        assert (directory / "dataset.json").is_file()
    assert screen.screen_structure("CCCN=[N+]=[N-]").flags
    assert genotox.screen_genotoxic_alerts(["CN(C)N=O"]).alerts
    assert ich.impurity_limit("Pd").limit is not None
