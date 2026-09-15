"""This server's own code holds no way to call out. Three lines, and every server ships them.

The scan covers the whole package — engine, tools and transport — because the rule is about the
server, not about one layer of it. `app.py` names loopback in its docstring, which the scanner
exempts on purpose: showing somebody how to reach the server they are running is documentation,
while naming somebody else's host is the thing being forbidden.
"""

from __future__ import annotations

from pathlib import Path

import chemclaw_mcp_kinetics
from mcp_server_kit.no_egress import assert_no_egress_sources

PACKAGE = Path(chemclaw_mcp_kinetics.__file__).parent


def test_no_module_can_reach_the_network() -> None:
    """No HTTP client imported, no remote host named — checked by AST, not by grep."""
    assert_no_egress_sources(PACKAGE)


def test_every_answer_is_computed_in_process_with_the_guard_armed() -> None:
    """The positive half, and this server earns it as cheaply as `thermalsafety` does.

    `props`, `chem` and `safety` prove sufficiency by pointing at a vendored, checksummed corpus,
    and `calc` by running each kind of calculation against compiled parameter data inside a wheel.
    This server has neither: every number is closed-form arithmetic over `math`, plus one
    transcribed allowance table defined in this package's own source, so there is nothing that
    *could* be fetched lazily. Running one of each kind of calculation with the guard armed (root
    `conftest.py`) is what turns that from an argument into a check — and it is the check that
    would catch a future dependency added here that fetches anything on first use.
    """
    from chemclaw_mcp_kinetics.engine.arrhenius import (
        activation_energy_from_two_points,
        rate_constant_at,
    )
    from chemclaw_mcp_kinetics.engine.reactors import (
        batch_conversion,
        cstr_conversion,
        pfr_conversion,
        semibatch_accumulation,
        time_for_batch_conversion,
    )
    from chemclaw_mcp_kinetics.engine.selftest import verify

    assert (
        rate_constant_at(
            35.0,
            reference_temperature_c=25.0,
            reference_rate_constant=1.0,
            activation_energy_kj_per_mol=52.9,
        ).rate_ratio
        > 1
    )
    assert (
        activation_energy_from_two_points(
            lower_temperature_c=25.0,
            lower_rate_constant=1e-4,
            upper_temperature_c=55.0,
            upper_rate_constant=1.6e-3,
        ).activation_energy_kj_per_mol
        > 0
    )
    assert batch_conversion(rate_constant=0.01, initial_concentration=1.0, time_seconds=60.0) > 0
    assert (
        time_for_batch_conversion(rate_constant=0.01, initial_concentration=1.0, conversion=0.5) > 0
    )
    assert (
        pfr_conversion(rate_constant=0.01, initial_concentration=1.0, residence_time_seconds=60.0)
        > 0
    )
    assert (
        cstr_conversion(
            rate_constant=0.01, initial_concentration=1.0, residence_time_seconds=60.0, order=2.0
        )
        > 0
    )
    assert (
        semibatch_accumulation(
            rate_constant=0.02,
            dose_time_seconds=600.0,
            initial_volume=10.0,
            dosed_moles=5.0,
            dosed_volume=1.0,
            initial_coreagent_concentration=0.8,
        ).peak_accumulation_fraction
        >= 0
    )
    # The readiness probe is the one path that touches a `Dataset`, so it is exercised too: a
    # dataset loader reaching for a file or a URL would fail here rather than in production.
    assert verify()
