"""This server's own code holds no way to call out.

The scan covers the whole package. A loopback address in `app.py`'s docstring is exempt by
design: naming the server you run is documentation, naming somebody else's host is forbidden.
"""

from __future__ import annotations

from pathlib import Path

import chemclaw_mcp_unitops
from mcp_server_kit.no_egress import assert_no_egress_sources

PACKAGE = Path(chemclaw_mcp_unitops.__file__).parent


def test_no_module_can_reach_the_network() -> None:
    """No HTTP client imported, no remote host named — checked by AST, not by grep."""
    assert_no_egress_sources(PACKAGE)


def test_every_answer_is_computed_in_process_with_the_guard_armed() -> None:
    """Every answer is computed in process, with the guard armed.

    Every number is closed-form arithmetic plus five published exponents in this package's source,
    so nothing could be fetched lazily; running one of each correlation under the armed guard checks
    that, and would catch a future dependency that fetches on first use.
    """
    from chemclaw_mcp_unitops.engine.crystallisation import crystallisation_yield
    from chemclaw_mcp_unitops.engine.distillation import shortcut_column
    from chemclaw_mcp_unitops.engine.drying import drying_time
    from chemclaw_mcp_unitops.engine.filtration import filtration_time
    from chemclaw_mcp_unitops.engine.heat import time_constant
    from chemclaw_mcp_unitops.engine.mixing import agitation_scale_up, just_suspended_speed
    from chemclaw_mcp_unitops.engine.selftest import verify

    assert (
        agitation_scale_up(
            small_impeller_diameter_m=0.05,
            small_speed_rpm=500.0,
            small_liquid_volume_m3=1.0e-3,
            large_impeller_diameter_m=0.45,
            large_liquid_volume_m3=0.25,
            power_number=1.5,
            liquid_density_kg_per_m3=880.0,
            liquid_viscosity_pa_s=1.2e-3,
        ).matched_power_per_volume.speed_rpm
        > 0
    )
    assert (
        just_suspended_speed(
            impeller_diameter_m=0.45,
            particle_diameter_m=1.5e-4,
            particle_density_kg_per_m3=1350.0,
            liquid_density_kg_per_m3=880.0,
            liquid_viscosity_pa_s=1.2e-3,
            solids_loading_percent=12.0,
            zwietering_constant=6.0,
            power_number=1.5,
            liquid_volume_m3=0.25,
        ).speed_rpm
        > 0
    )
    assert (
        time_constant(
            batch_mass_kg=220.0,
            heat_capacity_j_per_kg_k=1900.0,
            overall_heat_transfer_coefficient_w_per_m2_k=300.0,
            heat_transfer_area_m2=2.1,
            initial_temperature_c=60.0,
            jacket_temperature_c=-10.0,
        ).time_constant_seconds
        > 0
    )
    assert (
        shortcut_column(
            relative_volatility=2.5,
            light_key_in_feed=0.5,
            light_key_in_distillate=0.95,
            light_key_in_bottoms=0.05,
        ).theoretical_stages
        > 0
    )
    assert (
        crystallisation_yield(
            solute_charged_kg=3.0,
            solvent_charged_kg=10.0,
            solubility_hot_kg_per_kg_solvent=0.30,
            solubility_cold_kg_per_kg_solvent=0.05,
        ).yield_fraction
        > 0
    )
    assert (
        filtration_time(
            filtrate_volume_m3=0.25,
            filter_area_m2=0.456,
            pressure_drop_pa=8.0e4,
            filtrate_viscosity_pa_s=1.2e-3,
            specific_cake_resistance_m_per_kg=5.0e11,
            dry_cake_per_filtrate_kg_per_m3=120.0,
        ).total_time_seconds
        > 0
    )
    assert (
        drying_time(
            dry_solid_mass_kg=80.0,
            drying_area_m2=1.2,
            constant_rate_kg_per_m2_s=5.0e-4,
            initial_moisture_dry_basis=0.25,
            critical_moisture_dry_basis=0.10,
            final_moisture_dry_basis=0.005,
        ).total_time_seconds
        > 0
    )
    # The readiness probe is the one path that touches a `Dataset`, so it is exercised too: a
    # dataset loader reaching for a file or a URL would fail here rather than in production.
    assert verify()
