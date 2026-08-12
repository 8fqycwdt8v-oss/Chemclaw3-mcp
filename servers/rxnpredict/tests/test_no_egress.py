"""This server's own code holds no way to call out — the same three lines every server ships.

Worth more here than in `props`. This one carries model adapters whose upstream documentation is
full of download URLs, and those were moved into the README precisely because this scan refuses a
host literal in a module. The runtime guard covers what the adapters' *libraries* might do.
"""

from __future__ import annotations

from pathlib import Path

import chemclaw_mcp_rxnpredict
from mcp_server_kit.no_egress import assert_no_egress_sources

PACKAGE = Path(chemclaw_mcp_rxnpredict.__file__).parent


def test_no_module_can_reach_the_network() -> None:
    """No HTTP client imported, no remote host named — checked by AST, not by grep."""
    assert_no_egress_sources(PACKAGE)


def test_the_priors_come_from_the_vendored_corpus() -> None:
    """The weights behind every ranking are on disk, checksummed, and licensed."""
    from chemclaw_mcp_rxnpredict.engine.config import DATA_DIR
    from mcp_server_kit import load_dataset

    dataset = load_dataset(DATA_DIR, records_file="trust_priors.json")
    assert dataset.records_path.is_relative_to(PACKAGE)
    assert dataset.licence and dataset.retrieved_from
