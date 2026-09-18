"""Crystallisation yield, cake filtration and batch drying — the three isolation correlations.

One file because the three share a shape: each is a textbook model with a closed form somebody else
wrote down, and each is checked against that closed form rather than against a value recorded from a
run of this code.

- The **crystallisation** yield of a saturated charge is `1 - (S_cold/S_hot)(1 - E/W)`, which the
  mass balance does not contain.
- **Cake filtration** at constant pressure is parabolic in the filtrate volume, and the
  instantaneous rate it reports must be the derivative of the time it reports.
- The two **drying** periods are both `m_s·X_c/(A·N_c)` over their own characteristic span, by two
  separately written expressions, so they must agree at the critical moisture.
"""

from __future__ import annotations

import math

import pytest
from chemclaw_mcp_unitops.engine import crystallisation, drying, filtration
from chemclaw_mcp_unitops.engine.validation import UnitOpsInputError

CAKE = {
    "filter_area_m2": 0.456,
    "pressure_drop_pa": 8.0e4,
    "filtrate_viscosity_pa_s": 1.2e-3,
    "specific_cake_resistance_m_per_kg": 5.0e11,
    "dry_cake_per_filtrate_kg_per_m3": 120.0,
}
DRYER = {
    "dry_solid_mass_kg": 80.0,
    "drying_area_m2": 1.2,
    "constant_rate_kg_per_m2_s": 5.0e-4,
}


# --- Crystallisation --------------------------------------------------------------------------


def test_the_yield_is_the_hand_computed_mass_balance() -> None:
    """3 kg of solute in 10 kg of solvent, 0.30 kg/kg hot and 0.05 cold.

    By hand: the cold liquor holds 0.05 x 10 = 0.5 kg, so 2.5 kg crystallises and the yield is
    2.5/3.0 = 83.33%. The charge is exactly saturated hot (3.0 against 0.30 x 10).
    """
    found = crystallisation.crystallisation_yield(
        solute_charged_kg=3.0,
        solvent_charged_kg=10.0,
        solubility_hot_kg_per_kg_solvent=0.30,
        solubility_cold_kg_per_kg_solvent=0.05,
    )
    assert found.crystal_mass_kg == pytest.approx(2.5, rel=1.0e-12)
    assert found.mother_liquor_loss_kg == pytest.approx(0.5, rel=1.0e-12)
    assert found.yield_percent == pytest.approx(83.3333, abs=1.0e-4)
    assert found.saturation_at_start == pytest.approx(1.0, rel=1.0e-12)


@pytest.mark.parametrize(("cold", "evaporated"), [(0.05, 0.0), (0.05, 2.0), (0.02, 3.5)])
def test_a_saturated_charge_matches_the_closed_form(cold: float, evaporated: float) -> None:
    """`y = 1 - (S_cold/S_hot)(1 - E/W)`, which is nowhere in `crystallisation.py`."""
    solvent, hot = 10.0, 0.30
    found = crystallisation.crystallisation_yield(
        solute_charged_kg=hot * solvent,
        solvent_charged_kg=solvent,
        solubility_hot_kg_per_kg_solvent=hot,
        solubility_cold_kg_per_kg_solvent=cold,
        solvent_evaporated_kg=evaporated,
    )
    assert found.yield_fraction == pytest.approx(
        1.0 - (cold / hot) * (1.0 - evaporated / solvent), abs=1.0e-12
    )


def test_the_yield_goes_to_zero_as_the_two_solubilities_converge() -> None:
    """The limit the refusal sits just beyond: no temperature dependence, no crystallisation."""
    solvent, hot = 10.0, 0.30
    previous = 1.0
    for closeness in (0.5, 0.1, 1.0e-3, 1.0e-9):
        found = crystallisation.crystallisation_yield(
            solute_charged_kg=hot * solvent,
            solvent_charged_kg=solvent,
            solubility_hot_kg_per_kg_solvent=hot,
            solubility_cold_kg_per_kg_solvent=hot * (1.0 - closeness),
        ).yield_fraction
        assert found < previous
        previous = found
    assert previous == pytest.approx(0.0, abs=1.0e-8)


def test_a_flat_solubility_curve_is_refused_rather_than_yielding_nothing() -> None:
    """Equal solubilities is not a small yield but the wrong operation, and it says so."""
    with pytest.raises(UnitOpsInputError, match="anti-solvent"):
        crystallisation.crystallisation_yield(
            solute_charged_kg=3.0,
            solvent_charged_kg=10.0,
            solubility_hot_kg_per_kg_solvent=0.30,
            solubility_cold_kg_per_kg_solvent=0.30,
        )


def test_a_charge_that_never_dissolved_is_refused() -> None:
    """Above the hot solubility part of the charge was never in solution, so this is a slurry."""
    with pytest.raises(UnitOpsInputError, match="never goes into solution"):
        crystallisation.crystallisation_yield(
            solute_charged_kg=5.0,
            solvent_charged_kg=10.0,
            solubility_hot_kg_per_kg_solvent=0.30,
            solubility_cold_kg_per_kg_solvent=0.05,
        )


def test_a_batch_too_dilute_to_crystallise_answers_zero_rather_than_raising() -> None:
    """The answer here is "the batch is too dilute", which is a number and not an error."""
    found = crystallisation.crystallisation_yield(
        solute_charged_kg=0.4,
        solvent_charged_kg=10.0,
        solubility_hot_kg_per_kg_solvent=0.30,
        solubility_cold_kg_per_kg_solvent=0.05,
    )
    assert found.yield_fraction == 0.0
    assert found.crystal_mass_kg == 0.0
    assert found.mother_liquor_loss_kg == pytest.approx(0.4)
    assert found.saturation_at_start < 0.2


# --- Filtration -------------------------------------------------------------------------------


def test_the_filtration_time_is_the_hand_computed_one() -> None:
    """250 L through 0.456 m² at 0.8 bar, alpha = 5e11 m/kg, c = 120 kg/m³, R_m = 2e10 1/m.

    Cake term by hand: 1.2e-3 x 5e11 x 120 x 0.0625 / (2 x 0.207936 x 8e4) = 4.5e9/33269.8 =
    135 258 s. Medium term: 1.2e-3 x 2e10 x 0.25 / (0.456 x 8e4) = 6e6/36480 = 164.5 s.
    """
    found = filtration.filtration_time(
        filtrate_volume_m3=0.25, medium_resistance_per_m=2.0e10, **CAKE
    )
    assert found.cake_time_seconds == pytest.approx(135258.0, rel=1.0e-4)
    assert found.medium_time_seconds == pytest.approx(164.5, rel=1.0e-3)
    assert found.total_time_seconds == pytest.approx(
        found.cake_time_seconds + found.medium_time_seconds, rel=1.0e-12
    )
    assert found.cake_fraction_of_time > 0.99
    assert found.cake_mass_kg == pytest.approx(30.0, rel=1.0e-12)


def test_a_cake_limited_filtration_is_parabolic_in_volume() -> None:
    """Doubling the filtrate quadruples the time exactly, with no medium resistance in the way."""
    single = filtration.filtration_time(filtrate_volume_m3=0.1, **CAKE).total_time_seconds
    double = filtration.filtration_time(filtrate_volume_m3=0.2, **CAKE).total_time_seconds
    assert double / single == pytest.approx(4.0, rel=1.0e-12)


def test_a_medium_limited_filtration_is_linear_in_volume() -> None:
    """The other limit, which is what makes the returned split meaningful rather than decorative.

    With a negligible cake the time is the medium term alone, so doubling the volume doubles it.
    """
    thin = {**CAKE, "specific_cake_resistance_m_per_kg": 1.0, "medium_resistance_per_m": 2.0e10}
    single = filtration.filtration_time(filtrate_volume_m3=0.1, **thin)
    double = filtration.filtration_time(filtrate_volume_m3=0.2, **thin)
    assert double.total_time_seconds / single.total_time_seconds == pytest.approx(2.0, rel=1.0e-9)
    assert single.cake_fraction_of_time < 1.0e-6


def test_the_reported_rate_is_the_derivative_of_the_reported_time() -> None:
    """Darcy's differential law against a central difference of the integrated one."""
    volume, step = 0.25, 1.0e-7
    with_medium = {**CAKE, "medium_resistance_per_m": 2.0e10}
    reported = filtration.filtration_time(
        filtrate_volume_m3=volume, **with_medium
    ).final_rate_m3_per_s
    ahead = filtration.filtration_time(
        filtrate_volume_m3=volume + step, **with_medium
    ).total_time_seconds
    behind = filtration.filtration_time(
        filtrate_volume_m3=volume - step, **with_medium
    ).total_time_seconds
    assert reported == pytest.approx(2.0 * step / (ahead - behind), rel=1.0e-6)


def test_doubling_the_area_quarters_a_cake_limited_filtration() -> None:
    """`A²` in the denominator is what makes a filter's area the decision, and it is easy to get
    wrong by writing the diameter instead."""
    narrow = filtration.filtration_time(filtrate_volume_m3=0.25, **CAKE).total_time_seconds
    wide = filtration.filtration_time(
        filtrate_volume_m3=0.25, **{**CAKE, "filter_area_m2": 2.0 * CAKE["filter_area_m2"]}
    ).total_time_seconds
    assert narrow / wide == pytest.approx(4.0, rel=1.0e-12)


def test_a_missing_cake_resistance_cannot_be_defaulted() -> None:
    """alpha is the answer, so a caller without a filtration test gets a TypeError, not a guess."""
    with pytest.raises(TypeError):
        # The omission is the assertion: `specific_cake_resistance_m_per_kg` has no default, and
        # this call is here to prove a caller cannot leave it out. mypy reports exactly that, which
        # is why the ignore names `call-arg` — if the parameter ever gained a default the ignore
        # would go unused and this test would go red with it.
        filtration.filtration_time(  # type: ignore[call-arg]
            filtrate_volume_m3=0.25,
            filter_area_m2=0.456,
            pressure_drop_pa=8.0e4,
            filtrate_viscosity_pa_s=1.2e-3,
            dry_cake_per_filtrate_kg_per_m3=120.0,
        )


def test_a_zero_pressure_drop_is_refused() -> None:
    """No driving force is infinite time, not a very long one."""
    with pytest.raises(UnitOpsInputError, match="pressure drop"):
        filtration.filtration_time(filtrate_volume_m3=0.25, **{**CAKE, "pressure_drop_pa": 0.0})


# --- Drying -----------------------------------------------------------------------------------


def test_the_drying_time_is_the_hand_computed_one() -> None:
    """80 kg dry solid on 1.2 m² at 5e-4 kg/(m²·s), from X = 0.25 through X_c = 0.10 to X = 0.005.

    `m_s/(A·N_c)` = 80/(1.2 x 5e-4) = 133 333 s per unit of moisture. Constant period: x (0.25 -
    0.10) = 20 000 s. Falling period: x 0.10 x ln(0.10/0.005) = 13 333 x 2.99573 = 39 943 s. Total
    59 943 s = 16.65 h, and 19.6 kg of moisture leaves.
    """
    found = drying.drying_time(
        **DRYER,
        initial_moisture_dry_basis=0.25,
        critical_moisture_dry_basis=0.10,
        final_moisture_dry_basis=0.005,
    )
    assert found.constant_rate_seconds == pytest.approx(20000.0, rel=1.0e-9)
    assert found.falling_rate_seconds == pytest.approx(39943.0, rel=1.0e-4)
    assert found.total_time_hours == pytest.approx(16.65, abs=0.01)
    assert found.moisture_removed_kg == pytest.approx(19.6, rel=1.0e-12)
    assert found.starts_in_the_falling_rate_period is False


def test_the_two_periods_agree_at_the_critical_moisture() -> None:
    """Both are `m_s·X_c/(A·N_c)` over their own span, by two separately written expressions."""
    critical = 0.10
    constant_leg = drying.drying_time(
        **DRYER,
        initial_moisture_dry_basis=2.0 * critical,
        critical_moisture_dry_basis=critical,
        final_moisture_dry_basis=critical,
    ).constant_rate_seconds
    falling_leg = drying.drying_time(
        **DRYER,
        initial_moisture_dry_basis=critical,
        critical_moisture_dry_basis=critical,
        final_moisture_dry_basis=critical / math.e,
    ).falling_rate_seconds
    assert falling_leg == pytest.approx(constant_leg, rel=1.0e-12)


def test_a_charge_already_below_the_critical_moisture_has_no_constant_period() -> None:
    """The slower leg is the whole cycle, and the answer says so in a field of its own."""
    found = drying.drying_time(
        **DRYER,
        initial_moisture_dry_basis=0.08,
        critical_moisture_dry_basis=0.10,
        final_moisture_dry_basis=0.005,
    )
    assert found.constant_rate_seconds == 0.0
    assert found.starts_in_the_falling_rate_period is True
    assert found.falling_rate_seconds == pytest.approx(
        80.0 / (1.2 * 5.0e-4) * 0.10 * math.log(0.08 / 0.005), rel=1.0e-12
    )


def test_a_bone_dry_target_is_refused_because_this_model_never_reaches_it() -> None:
    """The falling rate goes linearly to zero at zero moisture, so zero takes infinite time."""
    with pytest.raises(UnitOpsInputError, match="infinite time"):
        drying.drying_time(
            **DRYER,
            initial_moisture_dry_basis=0.25,
            critical_moisture_dry_basis=0.10,
            final_moisture_dry_basis=0.0,
        )


def test_a_wet_basis_percentage_entered_as_a_target_is_refused() -> None:
    """A target wetter than the charge is the shape a basis mix-up takes, and it is named."""
    with pytest.raises(UnitOpsInputError, match="BONE-DRY"):
        drying.drying_time(
            **DRYER,
            initial_moisture_dry_basis=0.25,
            critical_moisture_dry_basis=0.10,
            final_moisture_dry_basis=0.30,
        )
