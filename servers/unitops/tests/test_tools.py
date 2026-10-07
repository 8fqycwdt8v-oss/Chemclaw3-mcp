"""The tool surface: what it returns beside the number, and what it refuses.

Holds the contract the model reads: every answer's `basis` naming its model and assumption, the
boundaries against `thermalsafety` and wash calculations stated in the surface, and domain
refusals arriving as `ValueError`. Asserted over the set, so a new tool without a `basis` fails.
"""

from __future__ import annotations

import asyncio

import pytest
from chemclaw_mcp_unitops import tools
from chemclaw_mcp_unitops.engine.validation import UnitOpsInputError

#: One well-formed call per served tool, with ordinary process numbers, so assertions below run
#: over the set.
CALLS = {
    "agitation_scale_up": {
        "small_impeller_diameter_m": 0.05,
        "small_speed_rpm": 500.0,
        "small_liquid_volume_m3": 1.0e-3,
        "large_impeller_diameter_m": 0.45,
        "large_liquid_volume_m3": 0.25,
        "power_number": 1.5,
        "liquid_density_kg_per_m3": 880.0,
        "liquid_viscosity_pa_s": 1.2e-3,
    },
    "just_suspended_speed": {
        "impeller_diameter_m": 0.45,
        "particle_diameter_m": 1.5e-4,
        "particle_density_kg_per_m3": 1350.0,
        "liquid_density_kg_per_m3": 880.0,
        "liquid_viscosity_pa_s": 1.2e-3,
        "solids_loading_percent": 12.0,
        "zwietering_constant": 6.0,
        "power_number": 1.5,
        "liquid_volume_m3": 0.25,
    },
    "heat_transfer_time_constant": {
        "batch_mass_kg": 220.0,
        "heat_capacity_j_per_kg_k": 1900.0,
        "overall_heat_transfer_coefficient_w_per_m2_k": 300.0,
        "heat_transfer_area_m2": 2.1,
        "initial_temperature_c": 60.0,
        "jacket_temperature_c": -10.0,
        "target_temperature_c": 5.0,
    },
    "shortcut_distillation": {
        "relative_volatility": 2.5,
        "light_key_in_feed": 0.5,
        "light_key_in_distillate": 0.95,
        "light_key_in_bottoms": 0.05,
    },
    "crystallisation_yield": {
        "solute_charged_kg": 3.0,
        "solvent_charged_kg": 10.0,
        "solubility_hot_kg_per_kg_solvent": 0.30,
        "solubility_cold_kg_per_kg_solvent": 0.05,
    },
    "filtration_time": {
        "filtrate_volume_m3": 0.25,
        "filter_area_m2": 0.456,
        "pressure_drop_pa": 8.0e4,
        "filtrate_viscosity_pa_s": 1.2e-3,
        "specific_cake_resistance_m_per_kg": 5.0e11,
        "dry_cake_per_filtrate_kg_per_m3": 120.0,
        "medium_resistance_per_m": 2.0e10,
    },
    "drying_time": {
        "dry_solid_mass_kg": 80.0,
        "drying_area_m2": 1.2,
        "constant_rate_kg_per_m2_s": 5.0e-4,
        "initial_moisture_dry_basis": 0.25,
        "critical_moisture_dry_basis": 0.10,
        "final_moisture_dry_basis": 0.005,
    },
}


def test_every_served_tool_has_a_worked_call_here() -> None:
    """Every served tool has a worked call here, read off the server.

    A transcribed list would agree with itself while a new tool went unasserted.
    """
    served = {tool.name for tool in asyncio.run(tools.server.list_tools())}
    assert set(CALLS) == served


@pytest.mark.parametrize("name", sorted(CALLS))
def test_every_tool_returns_a_basis_that_names_its_model_and_its_assumption(name: str) -> None:
    """Every tool returns a `basis` that names its model and its assumption.

    Long enough to be a sentence, and naming at least one assumption, the thing that decides
    whether the number applies.
    """
    answer = getattr(tools, name)(**CALLS[name])
    basis = answer.basis
    assert len(basis) > 80, f"{name}'s basis is a label rather than a statement: {basis!r}"
    assumption_words = (
        "assum",
        "constant",
        "ideal",
        "no ",
        "not ",
        "empirical",
        "incompressible",
        "equilibrium",
        "turbulent",
    )
    assert any(word in basis.lower() for word in assumption_words), (
        f"{name}'s basis names no assumption: {basis!r}"
    )


def test_the_jacket_answer_says_it_is_a_capacity_rather_than_a_load() -> None:
    """The pc-05 trap, and it has to be visible in the answer rather than only in the docstring."""
    answer = tools.heat_transfer_time_constant(**CALLS["heat_transfer_time_constant"])
    assert "not a process heat load" in answer.basis
    assert answer.initial_duty_w < 0.0
    assert answer.time_to_target_seconds is not None


def test_the_distillation_answer_says_its_correlation_is_empirical() -> None:
    """Gilliland's scatter is the thing a stage count from here must be read against."""
    answer = tools.shortcut_distillation(**CALLS["shortcut_distillation"])
    assert "EMPIRICAL" in answer.basis
    assert "no VLE data" in answer.basis
    assert answer.theoretical_stages > answer.minimum_stages
    assert answer.reflux_ratio > answer.minimum_reflux_ratio


def test_the_crystallisation_answer_says_it_is_a_maximum() -> None:
    """A yield a real batch comes in under, said in the answer the model quotes."""
    answer = tools.crystallisation_yield(**CALLS["crystallisation_yield"])
    assert "MAXIMUM" in answer.basis
    assert answer.yield_percent == pytest.approx(83.3333, abs=1.0e-4)


def test_the_filtration_answer_says_it_computes_no_wash() -> None:
    """pc-13's other half: a wash volume from here would be a rule of thumb wearing a number."""
    answer = tools.filtration_time(**CALLS["filtration_time"])
    assert "no wash" in answer.basis
    assert "INCOMPRESSIBLE" in answer.basis


def test_the_drying_answer_says_it_is_not_a_residual_solvent_specification() -> None:
    """A cycle that satisfies this model can still come off wet, and the answer says so."""
    answer = tools.drying_time(**CALLS["drying_time"])
    assert "not a residual-solvent specification" in answer.basis
    assert answer.total_time_hours == pytest.approx(16.65, abs=0.01)


def test_the_suspension_answer_says_what_its_criterion_is() -> None:
    """ "Just suspended" is a visual criterion about the base, not slurry homogeneity."""
    answer = tools.just_suspended_speed(**CALLS["just_suspended_speed"])
    assert "not slurry homogeneity" in answer.basis
    assert answer.speed_rpm > 0.0
    assert answer.power_per_volume_w_per_m3 > 0.0


def test_the_scale_up_answer_carries_both_criteria_and_their_disagreement() -> None:
    """One criterion returned alone would be this tool choosing, which is what it must not do."""
    answer = tools.agitation_scale_up(**CALLS["agitation_scale_up"])
    assert answer.matched_power_per_volume.speed_rpm != answer.matched_tip_speed.speed_rpm
    assert answer.criteria_disagree_by > 1.0
    assert answer.matched_power_per_volume.power_per_volume_w_per_m3 == pytest.approx(
        answer.small.power_per_volume_w_per_m3, rel=1.0e-12
    )
    assert answer.matched_tip_speed.tip_speed_m_per_s == pytest.approx(
        answer.small.tip_speed_m_per_s, rel=1.0e-12
    )


def test_no_tool_exports_a_thermal_safety_answer() -> None:
    """No tool exports a thermal-safety answer, asserted as an absence over the module.

    TMR, MTSR, T_D24 and criticality class belong to `servers/thermalsafety`; a tool name here
    containing one would be a second definition a model could reach.
    """
    forbidden = ("tmr", "mtsr", "criticality", "adiabatic", "runaway", "sadt")
    offenders = [
        name
        for name in dir(tools)
        if not name.startswith("_") and any(word in name.lower() for word in forbidden)
    ]
    assert not offenders, f"{offenders} belong to servers/thermalsafety, not here"


def test_no_tool_offers_a_wash_or_a_solubility_prediction() -> None:
    """No tool offers a wash volume or a solubility prediction.

    Neither has the data it would need (a displacement efficiency, a solubility curve) anywhere in
    this family.
    """
    forbidden = ("wash", "displacement", "predict_solubility", "solubility_curve", "metastable")
    offenders = [
        name
        for name in dir(tools)
        if not name.startswith("_") and any(word in name.lower() for word in forbidden)
    ]
    assert not offenders, f"{offenders} would be a number this server cannot derive"


@pytest.mark.parametrize(
    ("name", "changed", "message"),
    [
        ("shortcut_distillation", {"relative_volatility": 1.0}, "azeotrope"),
        ("crystallisation_yield", {"solubility_cold_kg_per_kg_solvent": 0.30}, "anti-solvent"),
        ("drying_time", {"final_moisture_dry_basis": 0.0}, "infinite time"),
        ("heat_transfer_time_constant", {"target_temperature_c": 90.0}, "never reaches it"),
        ("just_suspended_speed", {"particle_density_kg_per_m3": 700.0}, "does not settle"),
        ("filtration_time", {"pressure_drop_pa": 0.0}, "pressure drop"),
    ],
)
def test_a_refusal_reaches_the_model_as_a_value_error_naming_the_problem(
    name: str, changed: dict[str, float], message: str
) -> None:
    """A refusal reaches the model as a `ValueError` naming the problem.

    `connector_app` passes `ValueError` through verbatim and replaces anything else with an opaque
    error id.
    """
    with pytest.raises(UnitOpsInputError, match=message) as raised:
        getattr(tools, name)(**{**CALLS[name], **changed})
    assert isinstance(raised.value, ValueError)
