"""The <621> adjustment allowances, and the two places the obvious reading is wrong.

A transcribed regulatory table cannot be checked against a derivation the way the peak formulas
can, so what is asserted here is the *behaviour at each boundary* — which is where a transcription
error shows up — plus the two rows whose rule is not the one a reader assumes.
"""

from __future__ import annotations

import pytest
from chemclaw_mcp_suitability.engine import adjustments


def test_a_change_exactly_at_the_limit_is_permitted_rather_than_failed_by_rounding() -> None:
    """1.0 -> 1.5 mL/min is exactly the 50% allowance, and a chemist writes it in those digits.

    Binary floating point does not always agree that 1.5 - 1.0 equals 0.5 * 1.0, and a method
    change refused for that reason would be refused for a reason nobody could act on.
    """
    verdict = adjustments.check_adjustment("flow_rate", 1.0, 1.5)
    assert verdict.permitted is True
    assert verdict.limit_value == pytest.approx(1.5)


def test_just_past_the_limit_is_refused() -> None:
    """The other side of the same boundary, which makes the first assertion mean something."""
    assert adjustments.check_adjustment("flow_rate", 1.0, 1.51).permitted is False


def test_the_minor_component_cap_binds_the_other_way_round_from_the_obvious_reading() -> None:
    """30% relative, capped at 10 absolute — and at 5% organic the *relative* bound is tighter.

    The obvious misreading is that the 10-point cap is the operative limit, so 5% may go to 15%.
    It may go to 6.5%. The two bounds swap which one binds depending on the starting proportion,
    which is why both are carried and why the answer names the one that applied.
    """
    tight = adjustments.check_adjustment("minor_mobile_phase_component", 5.0, 6.5)
    assert tight.permitted is True
    assert (
        adjustments.check_adjustment("minor_mobile_phase_component", 5.0, 15.0).permitted is False
    )
    assert tight.limit_value == pytest.approx(6.5)


def test_at_a_large_proportion_the_absolute_cap_is_what_binds() -> None:
    """At 40% organic, 30% relative would be 12 points; the cap holds it to 10, and says so."""
    verdict = adjustments.check_adjustment("minor_mobile_phase_component", 40.0, 50.0)
    assert verdict.permitted is True
    assert "absolute cap" in verdict.reason
    assert (
        adjustments.check_adjustment("minor_mobile_phase_component", 40.0, 52.0).permitted is False
    )


def test_particle_size_may_be_reduced_but_never_increased() -> None:
    """A one-directional allowance, which a symmetric tolerance would get wrong half the time."""
    assert adjustments.check_adjustment("particle_size", 5.0, 3.0).permitted is True
    assert adjustments.check_adjustment("particle_size", 5.0, 2.4).permitted is False  # past 50%
    assert adjustments.check_adjustment("particle_size", 5.0, 5.1).permitted is False


def test_the_detector_wavelength_permits_no_adjustment_at_all() -> None:
    """Not a wide tolerance — none. A change alters every analyte's response factor differently."""
    assert adjustments.check_adjustment("detector_wavelength", 254.0, 254.0).permitted is True
    assert adjustments.check_adjustment("detector_wavelength", 254.0, 255.0).permitted is False


def test_injection_volume_may_be_reduced_with_no_numeric_floor_and_the_answer_says_so() -> None:
    """The floor is a judgement about this method's precision, which no table can supply.

    Reporting `limit_value=None` rather than a number is the honest form: a number here would be
    invented.
    """
    verdict = adjustments.check_adjustment("injection_volume", 20.0, 5.0)
    assert verdict.permitted is True
    assert verdict.limit_value is None
    assert adjustments.check_adjustment("injection_volume", 20.0, 25.0).permitted is False


def test_ph_is_an_absolute_allowance_not_a_relative_one() -> None:
    """+-0.2 units at pH 3 and at pH 9 alike; a relative reading would give 0.6 at pH 3."""
    assert adjustments.check_adjustment("mobile_phase_ph", 3.0, 3.2).permitted is True
    assert adjustments.check_adjustment("mobile_phase_ph", 3.0, 3.3).permitted is False
    assert adjustments.check_adjustment("mobile_phase_ph", 9.0, 9.2).permitted is True


def test_a_gradient_is_refused_by_name_rather_than_evaluated_against_the_isocratic_table() -> None:
    """The refusal that matters most, because the approximation here would be permissive.

    A gradient's selectivity depends on the instrument's dwell volume as well as on the method, so
    an isocratic allowance applied to one would bless a change that shifts the separation on a
    different system — the failure mode being permissive is why this refuses instead of warning.
    """
    with pytest.raises(adjustments.AdjustmentError, match="isocratic"):
        adjustments.check_adjustment("flow_rate", 1.0, 1.2, is_gradient=True)


def test_a_relative_allowance_against_a_zero_original_is_refused_rather_than_dividing() -> None:
    """0% organic has no 30% of itself, and the alternative is a bound of zero that looks real."""
    with pytest.raises(adjustments.AdjustmentError, match="nothing to take a percentage of"):
        adjustments.check_adjustment("minor_mobile_phase_component", 0.0, 2.0)


def test_a_negative_value_is_refused_naming_the_parameter() -> None:
    """No method parameter here can be negative, and the name is what locates the typo."""
    with pytest.raises(adjustments.AdjustmentError, match="flow rate"):
        adjustments.check_adjustment("flow_rate", 1.0, -0.5)


def test_every_parameter_in_the_literal_type_has_a_row_in_the_table() -> None:
    """A name addable to the `Parameter` union without a row would `KeyError` at call time.

    Checked in both directions, so a row nobody can address is caught as well as a name with no
    row — the same shape as the fleet's other declaration-versus-surface guards.
    """
    from typing import get_args

    declared = set(get_args(adjustments.Parameter))
    tabulated = set(adjustments.ADJUSTMENTS)
    assert declared == tabulated, (
        f"declared but not tabulated: {sorted(declared - tabulated)}; "
        f"tabulated but not addressable: {sorted(tabulated - declared)}"
    )


def test_every_row_states_a_unit_or_is_explicitly_dimensionless() -> None:
    """A tolerance with no unit is the mistake this fleet's docstring rule exists to prevent."""
    dimensionless = {"buffer_concentration"}  # a relative change to a concentration
    for name, allowance in adjustments.ADJUSTMENTS.items():
        if name in dimensionless:
            assert allowance.unit == ""
        else:
            assert allowance.unit, f"{name} states no unit"


def test_an_unchanged_parameter_is_permitted_for_every_row_including_the_forbidden_one() -> None:
    """Proposing no change is never an adjustment, anywhere in the table."""
    for name in adjustments.ADJUSTMENTS:
        verdict = adjustments.check_adjustment(name, 10.0, 10.0)
        assert verdict.permitted is True, f"{name} refused an unchanged value"
