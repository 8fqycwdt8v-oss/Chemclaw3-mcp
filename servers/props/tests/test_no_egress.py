"""This server's own code holds no way to call out.

The scan covers the whole package. A loopback address in `app.py`'s docstring is exempt by
design: naming the server you run is documentation, naming somebody else's host is forbidden.
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
