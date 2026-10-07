"""Arrhenius arithmetic, and the boundary against the extrapolation `thermalsafety` already owns."""

from __future__ import annotations

import math

import pytest
from chemclaw_mcp_kinetics.engine import arrhenius


def test_determining_ea_from_two_points_and_extrapolating_back_is_a_round_trip() -> None:
    """The two operations are inverses, so composing them must return the input exactly.

    Both are closed-form algebra on one line, so any discrepancy is a sign or unit error.
    """
    pair = arrhenius.activation_energy_from_two_points(
        lower_temperature_c=25.0,
        lower_rate_constant=1.0e-4,
        upper_temperature_c=55.0,
        upper_rate_constant=1.6e-3,
    )
    carried = arrhenius.rate_constant_at(
        55.0,
        reference_temperature_c=25.0,
        reference_rate_constant=1.0e-4,
        activation_energy_kj_per_mol=pair.activation_energy_kj_per_mol,
    )
    assert carried.rate_constant == pytest.approx(1.6e-3, rel=1e-12)
    assert carried.rate_ratio == pytest.approx(16.0, rel=1e-12)


def test_ln_a_is_consistent_with_both_points_not_just_the_one_it_was_taken_at() -> None:
    """`ln A` is computed at the lower point; the line passes through both by construction.

    Reproducing the upper point too catches an error a round trip through the same endpoint would
    hide.
    """
    pair = arrhenius.activation_energy_from_two_points(
        lower_temperature_c=10.0,
        lower_rate_constant=3.0e-5,
        upper_temperature_c=80.0,
        upper_rate_constant=9.0e-3,
    )
    upper_k = arrhenius.kelvin(80.0)
    predicted = math.exp(
        pair.ln_pre_exponential
        - pair.activation_energy_kj_per_mol
        * 1000.0
        / (arrhenius.GAS_CONSTANT_J_PER_MOL_K * upper_k)
    )
    assert predicted == pytest.approx(9.0e-3, rel=1e-9)


def test_the_textbook_rule_of_thumb_falls_out_at_the_activation_energy_it_belongs_to() -> None:
    """ "The rate doubles every 10 °C" is a statement about E_a ~ 53 kJ/mol near room temperature.

    Worth pinning because it is the one Arrhenius fact every chemist carries, and a module that
    disagreed with it would be wrong in a way nobody would notice from the formula.
    """
    carried = arrhenius.rate_constant_at(
        35.0,
        reference_temperature_c=25.0,
        reference_rate_constant=1.0,
        activation_energy_kj_per_mol=52.9,
    )
    assert carried.rate_ratio == pytest.approx(2.0, abs=0.01)


def test_a_wide_extrapolation_is_flagged_rather_than_refused() -> None:
    """How far is too far depends on whether the mechanism changes, which arithmetic cannot know.

    So the distance is reported and the caller decides — refusing would be this tool claiming
    knowledge about the reaction it does not have.
    """
    near = arrhenius.rate_constant_at(
        45.0,
        reference_temperature_c=25.0,
        reference_rate_constant=1.0,
        activation_energy_kj_per_mol=80.0,
    )
    far = arrhenius.rate_constant_at(
        145.0,
        reference_temperature_c=25.0,
        reference_rate_constant=1.0,
        activation_energy_kj_per_mol=80.0,
    )
    assert near.far_from_the_measurement is False
    assert far.far_from_the_measurement is True
    assert far.extrapolated_by_k == pytest.approx(120.0)


def test_the_extrapolation_distance_is_signed_so_up_and_down_are_distinguishable() -> None:
    """Heating and cooling are not equally safe: a mechanism that switches usually does so warm."""
    down = arrhenius.rate_constant_at(
        5.0,
        reference_temperature_c=25.0,
        reference_rate_constant=1.0,
        activation_energy_kj_per_mol=80.0,
    )
    assert down.extrapolated_by_k == pytest.approx(-20.0)
    assert down.rate_ratio < 1.0


def test_a_narrow_span_is_flagged_because_measurement_error_dominates_there() -> None:
    """Two points 5 K apart determine an E_a swamped by the error in the two rate constants."""
    narrow = arrhenius.activation_energy_from_two_points(
        lower_temperature_c=25.0,
        lower_rate_constant=1.0e-4,
        upper_temperature_c=30.0,
        upper_rate_constant=1.3e-4,
    )
    wide = arrhenius.activation_energy_from_two_points(
        lower_temperature_c=25.0,
        lower_rate_constant=1.0e-4,
        upper_temperature_c=65.0,
        upper_rate_constant=3.0e-3,
    )
    assert narrow.span_is_narrow is True
    assert wide.span_is_narrow is False


def test_a_narrow_span_really_does_amplify_error_which_is_why_the_flag_exists() -> None:
    """The flag's justification, measured rather than asserted.

    A 5% error in the upper rate constant moves E_a by far more over a 5 K span than over a 40 K
    one. This is the arithmetic behind the warning, so the warning is not a feeling.
    """

    def energy(upper_c: float, upper_k_value: float) -> float:
        return arrhenius.activation_energy_from_two_points(
            lower_temperature_c=25.0,
            lower_rate_constant=1.0e-4,
            upper_temperature_c=upper_c,
            upper_rate_constant=upper_k_value,
        ).activation_energy_kj_per_mol

    narrow_shift = abs(energy(30.0, 1.3e-4 * 1.05) - energy(30.0, 1.3e-4)) / energy(30.0, 1.3e-4)
    wide_shift = abs(energy(65.0, 3.0e-3 * 1.05) - energy(65.0, 3.0e-3)) / energy(65.0, 3.0e-3)
    assert narrow_shift > 5.0 * wide_shift, (narrow_shift, wide_shift)


def test_a_rate_constant_that_falls_with_temperature_is_refused_by_name() -> None:
    """A rate constant that falls with temperature is refused, not reported as a negative E_a.

    The likely causes (pre-equilibrium, mechanism change, catalyst decomposition, inconsistent
    points) are what the chemist needs told.
    """
    with pytest.raises(arrhenius.KineticsInputError, match="rate that falls"):
        arrhenius.activation_energy_from_two_points(
            lower_temperature_c=25.0,
            lower_rate_constant=2.0e-3,
            upper_temperature_c=55.0,
            upper_rate_constant=1.0e-3,
        )


def test_two_points_at_the_same_temperature_are_refused_as_undefined_not_as_large() -> None:
    """No span means no temperature dependence to measure; dividing would give infinity."""
    with pytest.raises(arrhenius.KineticsInputError, match="undefined rather than large"):
        arrhenius.activation_energy_from_two_points(
            lower_temperature_c=25.0,
            lower_rate_constant=1.0e-4,
            upper_temperature_c=25.0,
            upper_rate_constant=2.0e-4,
        )


def test_a_kelvin_figure_entered_as_celsius_is_refused_with_the_units_mistake_named() -> None:
    """298 K typed as -273 °C is the realistic error, and it is below absolute zero."""
    with pytest.raises(arrhenius.KineticsInputError, match="takes °C"):
        arrhenius.kelvin(-300.0)


def test_this_module_returns_no_tmr_and_no_temperature() -> None:
    """The boundary against `servers/thermalsafety`, asserted over the surface.

    `thermalsafety` extrapolates a decomposition's heat-release rate and returns a temperature
    (TMR); this module extrapolates the reaction's rate constant and returns a rate constant. A
    temperature or TMR tool here would duplicate that server and invite quoting a decomposition's
    E_a for the synthesis. Checked by name, since no import can tie two servers.
    """
    public = {name for name in dir(arrhenius) if not name.startswith("_")}
    for forbidden in ("tmr", "temperature_for", "d24", "criticality", "adiabatic", "runaway"):
        assert not any(forbidden in name.lower() for name in public), (forbidden, sorted(public))


@pytest.mark.parametrize("bad", [math.inf, -math.inf, math.nan])
@pytest.mark.parametrize(
    "argument",
    ["target_temperature_c", "reference_rate_constant", "reference_temperature_c", "activation"],
)
def test_a_non_finite_input_is_refused_by_name_rather_than_propagated(
    argument: str, bad: float
) -> None:
    """`value <= 0.0` is False for NaN and infinity, and pydantic's `gt=0` passes infinity.

    So these reached the arithmetic and came back as `inf`/`nan` in the answer, or as an exception
    `connector_app` replaces with an opaque `error_id`.
    """
    values = {
        "target_temperature_c": 60.0,
        "reference_rate_constant": 1.0e-3,
        "reference_temperature_c": 25.0,
        "activation": 80.0,
    }
    values[argument] = bad
    with pytest.raises(arrhenius.KineticsInputError, match="finite"):
        arrhenius.rate_constant_at(
            values["target_temperature_c"],
            reference_rate_constant=values["reference_rate_constant"],
            reference_temperature_c=values["reference_temperature_c"],
            activation_energy_kj_per_mol=values["activation"],
        )


def test_an_extrapolation_that_overflows_is_refused_by_name() -> None:
    """From 0.15 K to 1000 °C at 1000 kJ/mol the ratio is `exp(~8e5)`: `math.exp` raised
    `OverflowError`, which `connector_app` replaces with an opaque `error_id`."""
    with pytest.raises(arrhenius.KineticsInputError, match="overflows a double"):
        arrhenius.rate_constant_at(
            1000.0,
            reference_temperature_c=-273.0,
            reference_rate_constant=1.0,
            activation_energy_kj_per_mol=1000.0,
        )


def test_an_activation_energy_that_overflows_is_refused_rather_than_returned_as_infinity() -> None:
    """Two points 1e-9 K apart spanning 600 decades would give an infinite E_a; it is refused
    instead.
    """
    with pytest.raises(arrhenius.KineticsInputError, match="overflows a double"):
        arrhenius.activation_energy_from_two_points(
            lower_temperature_c=0.0,
            lower_rate_constant=1e-300,
            upper_temperature_c=1e-9,
            upper_rate_constant=1e300,
        )
