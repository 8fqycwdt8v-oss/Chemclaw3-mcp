"""This server's own code holds no way to call out. Three lines, and every server ships them.

The scan covers the whole package — engine, tools and transport — because the rule is about the
server, not about one layer of it. `app.py` names loopback in its docstring, which the scanner
exempts on purpose: showing somebody how to reach the server they are running is documentation,
while naming somebody else's host is the thing being forbidden.
"""

from __future__ import annotations

from pathlib import Path

import chemclaw_mcp_thermalsafety
from mcp_server_kit.no_egress import assert_no_egress_sources

PACKAGE = Path(chemclaw_mcp_thermalsafety.__file__).parent


def test_no_module_can_reach_the_network() -> None:
    """No HTTP client imported, no remote host named — checked by AST, not by grep."""
    assert_no_egress_sources(PACKAGE)


def test_every_answer_is_computed_in_process_with_the_guard_armed() -> None:
    """The positive half, and this server earns it more cheaply than any other in the fleet.

    `props`, `chem` and `safety` prove sufficiency by pointing at a vendored, checksummed corpus,
    and `calc` by running each kind of calculation against compiled parameter data inside a wheel.
    This server has neither: every number is closed-form arithmetic over `math` and a
    seventeen-element dict defined in this package's own source, so there is nothing that *could*
    be fetched lazily. Running one of each kind of calculation with the guard armed (root
    `conftest.py`) is what turns that from an argument into a check — and it is the check that
    would catch a future dependency being added here that fetches anything on first use.
    """
    from chemclaw_mcp_thermalsafety.engine.oxygen_balance import oxygen_balance
    from chemclaw_mcp_thermalsafety.engine.runaway import (
        adiabatic_temperature_rise,
        stoessel_class,
        temperature_for_tmr,
    )
    from chemclaw_mcp_thermalsafety.engine.semenov import semenov_criticality

    assert (
        adiabatic_temperature_rise(
            heat_of_reaction_kj_per_mol=-150.0,
            moles=10.0,
            mass_kg=50.0,
            specific_heat_kj_per_kg_k=1.9,
        )
        > 0
    )
    assert (
        temperature_for_tmr(
            target_hours=24.0,
            reference_temperature_c=200.0,
            heat_release_rate_w_per_kg=50.0,
            activation_energy_kj_per_mol=120.0,
            specific_heat_kj_per_kg_k=1.9,
        )
        < 200.0
    )
    assert (
        semenov_criticality(
            mass_kg=25.0,
            heat_release_rate_w_per_kg=1.0,
            reference_temperature_c=100.0,
            activation_energy_kj_per_mol=140.0,
            heat_transfer_coefficient_w_per_m2_k=5.0,
            surface_area_m2=0.5,
        ).critical_ambient_c
        > 0
    )
    assert oxygen_balance("C3H5N3O9").oxygen_balance_percent > 0
    assert (
        stoessel_class(
            process_temperature_c=20.0,
            mtsr_c=150.0,
            max_technical_temperature_c=160.0,
            decomposition_t_d24_c=140.0,
        ).criticality_class
        == 5
    )
