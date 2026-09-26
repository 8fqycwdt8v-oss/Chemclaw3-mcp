"""The jacketed-vessel time constant, against the identities every first-order system satisfies.

Nothing here is checked against a recorded number except the one hand computation at the top. The
rest are relations — 63.2% at `t = τ`, a half-life of `τ·ln 2`, the inversion round-tripping — which
tie the time constant to the two functions derived from it and cannot be satisfied by any of the
three agreeing with itself.
"""

from __future__ import annotations

import math

import pytest
from chemclaw_mcp_unitops.engine import heat
from chemclaw_mcp_unitops.engine.validation import UnitOpsInputError

VESSEL = {
    "batch_mass_kg": 200.0,
    "heat_capacity_j_per_kg_k": 1900.0,
    "overall_heat_transfer_coefficient_w_per_m2_k": 300.0,
    "heat_transfer_area_m2": 2.1,
    "initial_temperature_c": 60.0,
    "jacket_temperature_c": 5.0,
}


def test_the_time_constant_is_the_hand_computed_one() -> None:
    """τ = M·c_p/(U·A) = 200 x 1900 / (300 x 2.1) = 380 000/630 = 603.17 s = 10.05 min."""
    found = heat.time_constant(**VESSEL)
    assert found.time_constant_seconds == pytest.approx(603.17, abs=0.01)
    assert found.time_constant_minutes == pytest.approx(10.053, abs=1.0e-3)


def test_the_initial_duty_is_ua_times_the_starting_gap_and_is_signed() -> None:
    """300 x 2.1 x 55 = 34 650 W, negative because the batch is losing heat to the jacket."""
    assert heat.time_constant(**VESSEL).initial_duty_w == pytest.approx(-34650.0, rel=1.0e-12)


def test_a_heat_up_reports_a_positive_duty() -> None:
    """The sign is the direction, and a reader has to be able to tell the two apart."""
    heating = {**VESSEL, "initial_temperature_c": 20.0, "jacket_temperature_c": 80.0}
    assert heat.time_constant(**heating).initial_duty_w > 0.0


def test_the_batch_has_closed_63_2_percent_of_its_gap_at_one_time_constant() -> None:
    """The textbook number, and the one identity that defines what a time constant *is*."""
    found = heat.time_constant(**VESSEL)
    tau = found.time_constant_seconds
    at_tau = heat.time_constant(**VESSEL, time_seconds=tau).temperature_after_c
    assert at_tau is not None
    assert (60.0 - at_tau) / 55.0 == pytest.approx(0.6321205588, abs=1.0e-9)
    assert found.approach_at_one_time_constant == pytest.approx(1.0 - 1.0 / math.e, abs=1.0e-15)


def test_the_half_life_is_tau_times_ln_two() -> None:
    """The inversion and the forward form are separately written; this is what ties them."""
    tau = heat.time_constant(**VESSEL).time_constant_seconds
    half = heat.time_constant(**VESSEL, target_temperature_c=5.0 + 27.5).time_to_target_seconds
    assert half is not None
    assert half == pytest.approx(tau * math.log(2.0), rel=1.0e-12)


def test_the_two_inversions_round_trip() -> None:
    """A temperature at a time, put back as a target, must give the time back."""
    at_800 = heat.time_constant(**VESSEL, time_seconds=800.0).temperature_after_c
    assert at_800 is not None
    back = heat.time_constant(**VESSEL, target_temperature_c=at_800).time_to_target_seconds
    assert back == pytest.approx(800.0, rel=1.0e-9)


def test_three_time_constants_close_95_percent_of_the_gap() -> None:
    """The rule of thumb an engineer carries, which is `1 - e^-3`."""
    tau = heat.time_constant(**VESSEL).time_constant_seconds
    at_three = heat.time_constant(**VESSEL, time_seconds=3.0 * tau).temperature_after_c
    assert at_three is not None
    assert (60.0 - at_three) / 55.0 == pytest.approx(0.9502, abs=1.0e-4)


def test_doubling_the_batch_doubles_the_time_constant() -> None:
    """Mass over area is why a cool-down scales badly, and it is the whole point of the tool."""
    small = heat.time_constant(**VESSEL).time_constant_seconds
    large = heat.time_constant(**{**VESSEL, "batch_mass_kg": 400.0}).time_constant_seconds
    assert large / small == pytest.approx(2.0, rel=1.0e-12)


def test_a_target_past_the_jacket_is_refused_as_unreachable() -> None:
    """A jacket carries a batch towards itself and no further: not a long time, but none."""
    with pytest.raises(UnitOpsInputError, match="never reaches it"):
        heat.time_constant(**VESSEL, target_temperature_c=70.0)
    with pytest.raises(UnitOpsInputError, match="asymptotically"):
        heat.time_constant(**VESSEL, target_temperature_c=-10.0)


def test_the_jacket_temperature_itself_is_refused_rather_than_returned_as_infinity() -> None:
    """The boundary case of the same rule, where `ln 0` would otherwise be evaluated."""
    with pytest.raises(UnitOpsInputError, match="asymptotically"):
        heat.time_constant(**VESSEL, target_temperature_c=5.0)


def test_no_driving_force_is_refused_with_the_reason_named() -> None:
    """A batch already at its jacket temperature has no transient to describe."""
    with pytest.raises(UnitOpsInputError, match="no driving force"):
        heat.time_constant(**{**VESSEL, "initial_temperature_c": 5.0})


def test_a_kelvin_figure_entered_as_celsius_is_refused() -> None:
    """-250 °C is below absolute zero and is almost always 23 K written in the wrong box."""
    with pytest.raises(UnitOpsInputError, match="absolute zero"):
        heat.time_constant(**{**VESSEL, "jacket_temperature_c": -300.0})


def test_a_missing_heat_transfer_coefficient_cannot_be_defaulted() -> None:
    """`U` has no default, which is what stops a computed duty resting on an assumed one."""
    without = dict(VESSEL)
    del without["overall_heat_transfer_coefficient_w_per_m2_k"]
    with pytest.raises(TypeError):
        heat.time_constant(**without)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -1.0])
def test_a_time_that_is_negative_or_not_finite_is_refused(bad: float) -> None:
    """`time_seconds < 0.0` is False for NaN, which came back as a `null` temperature beside tau."""
    with pytest.raises(UnitOpsInputError, match="the time"):
        heat.time_constant(**VESSEL, time_seconds=bad)
