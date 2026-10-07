"""This server's own code holds no way to call out.

The scan covers the whole package. A loopback address in `app.py`'s docstring is exempt by
design: naming the server you run is documentation, naming somebody else's host is forbidden.
"""

from __future__ import annotations

from pathlib import Path

import chemclaw_mcp_suitability
from mcp_server_kit.no_egress import assert_no_egress_sources

PACKAGE = Path(chemclaw_mcp_suitability.__file__).parent


def test_no_module_can_reach_the_network() -> None:
    """No HTTP client imported, no remote host named — checked by AST, not by grep."""
    assert_no_egress_sources(PACKAGE)


def test_every_answer_is_computed_in_process_with_the_guard_armed() -> None:
    """Every answer is computed in process, with the guard armed.

    Every number is closed-form arithmetic plus one allowance table in this package's source, so
    nothing could be fetched lazily; running one of each calculation under the armed guard checks
    that, and would catch a future dependency that fetches on first use.
    """
    from chemclaw_mcp_suitability.engine.adjustments import check_adjustment
    from chemclaw_mcp_suitability.engine.peaks import (
        plate_count,
        resolution,
        retention_factor,
        symmetry_factor,
    )
    from chemclaw_mcp_suitability.engine.precision import relative_standard_deviation
    from chemclaw_mcp_suitability.engine.selftest import verify

    assert plate_count(6.0, 0.2, "tangent").plates > 0
    assert resolution(5.0, 5.4, 0.2, 0.21, "tangent").resolution > 0
    assert symmetry_factor(0.4, 0.17).tailing_factor > 1
    assert retention_factor(6.0, 1.2).retention_factor > 0
    assert relative_standard_deviation([100.0, 101.0, 99.0]).mean > 0
    assert check_adjustment("flow_rate", 1.0, 1.2).permitted is True
    # The readiness probe is the one path that touches a `Dataset`, so it is exercised too: a
    # dataset loader that reached for a file or a URL would fail here rather than in production.
    assert verify()
