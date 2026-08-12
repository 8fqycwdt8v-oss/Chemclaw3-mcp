"""This server's own code holds no way to call out. Three lines, and every server ships them.

The scan covers the whole package — engine, tools and transport — because the rule is about the
server, not about one layer of it. `app.py` names loopback in its docstring, which the scanner
exempts on purpose: showing somebody how to reach the server they are running is documentation,
while naming somebody else's host is the thing being forbidden.
"""

from __future__ import annotations

from pathlib import Path

import chemclaw_mcp_props
from mcp_server_kit.no_egress import assert_no_egress_sources

PACKAGE = Path(chemclaw_mcp_props.__file__).parent


def test_no_module_can_reach_the_network() -> None:
    """No HTTP client imported, no remote host named — checked by AST, not by grep."""
    assert_no_egress_sources(PACKAGE)


def test_the_answers_come_from_the_vendored_corpus() -> None:
    """The positive half: the data this server serves is on disk, checksummed, and licensed."""
    from chemclaw_mcp_props.engine.records import dataset

    loaded = dataset()
    assert loaded.records_path.is_file()
    assert loaded.records_path.is_relative_to(PACKAGE)
    assert loaded.licence and loaded.retrieved_from
