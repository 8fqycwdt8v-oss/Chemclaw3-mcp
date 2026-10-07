"""This server's own code holds no way to call out.

The scan covers the whole package. A loopback address in `app.py`'s docstring is exempt by
design: naming the server you run is documentation, naming somebody else's host is forbidden.
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
    """Model weights are loaded from the image and never fetched by this package's code.

    RXNMapper ships its checkpoint in its wheel. This asserts the code-level half: nothing here asks
    for a model by URL or triggers a download. The build-time load check (Containerfile) and runtime
    denial (NetworkPolicy) are asserted elsewhere.
    """
    from chemclaw_mcp_rxnlabel.engine import mapping, naming, version

    # Absent or present, the version says which — so a deployment whose model failed to load is
    # visible in every label it produces rather than only in a log line nobody reads.
    components = version.components()
    assert (components["atom_mapper"] == "absent") is not mapping.available()
    assert (components["reaction_namer"] == "absent") is not naming.available()
