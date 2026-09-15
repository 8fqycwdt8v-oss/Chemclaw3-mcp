"""The reactors, checked against relations that hold independently of this code.

The strong tests here are not "does the exponential return an exponential". They are the ones that
use a relation the implementation does not contain: a textbook ratio, an exact quadratic root, an
inverse that must round-trip, and a convergence *order* — which is the one that caught a real bug.
"""

from __future__ import annotations

import math

import pytest
from chemclaw_mcp_kinetics.engine import reactors
from chemclaw_mcp_kinetics.engine.arrhenius import KineticsInputError

#: The worked semi-batch case used throughout: an acid chloride dosed into an amine over two hours.
#:
#: Passed through `_dose` with explicit keywords rather than splatted. A `**{**DOSE, ...}` splat is
#: a float-valued mapping, so a type checker must assume it could supply *any* parameter — including
#: `steps`, the one `int` in that signature — and reports every such call as mis-typed. That is true
#: of the type and false of every call here, and annotating the dict does not fix it because the
#: mapping is still float-valued. Naming the arguments is what makes the fixture and the signature
#: agree, and it is what a reader gets to see at each call site anyway.
_RATE_CONSTANT = 0.02
_DOSE_TIME_SECONDS = 7200.0
_INITIAL_VOLUME = 50.0
_DOSED_MOLES = 40.0
_DOSED_VOLUME = 8.0
_COREAGENT_CONCENTRATION = 0.9


def _dose(
    *,
    rate_constant: float = _RATE_CONSTANT,
    dose_time_seconds: float = _DOSE_TIME_SECONDS,
    steps: int = reactors.DEFAULT_INTEGRATION_STEPS,
) -> reactors.SemiBatchProfile:
    """The worked case, with the parameters these tests vary."""
    return reactors.semibatch_accumulation(
        rate_constant=rate_constant,
        dose_time_seconds=dose_time_seconds,
        initial_volume=_INITIAL_VOLUME,
        dosed_moles=_DOSED_MOLES,
        dosed_volume=_DOSED_VOLUME,
        initial_coreagent_concentration=_COREAGENT_CONCENTRATION,
        steps=steps,
    )


def test_a_cstr_needs_three_point_nine_times_a_pfr_at_ninety_percent_first_order() -> None:
    """The textbook ratio, which neither function contains and both must agree with.

    `τ_CSTR/τ_PFR = X/((1-X)·ln(1/(1-X)))`, which at X = 0.9 is 3.909. A chemist moving a batch
    process to continuous meets this number, and it is the clearest single statement of why a
    perfectly mixed tank is expensive: the whole reactor sits at the outlet composition, so at the
    slowest rate in the system.
    """
    rate = 0.01
    plug = reactors.time_for_batch_conversion(
        rate_constant=rate, initial_concentration=1.0, conversion=0.9
    )
    # From X = kτ/(1+kτ), inverted — written independently rather than taken from the module.
    tank = 0.9 / (rate * 0.1)
    assert tank / plug == pytest.approx(3.909, abs=0.001)


@pytest.mark.parametrize("order", [0.0, 0.5, 1.0, 1.5, 2.0])
def test_the_time_and_the_conversion_are_exact_inverses_at_every_order(order: float) -> None:
    """Both are closed form, so they must invert to machine precision rather than approximately.

    This is the property that makes the pair safe to offer: a solver and its forward model drift at
    the fourth decimal, and two closed forms cannot.
    """
    seconds = reactors.time_for_batch_conversion(
        rate_constant=0.02, initial_concentration=2.0, conversion=0.75, order=order
    )
    assert reactors.batch_conversion(
        rate_constant=0.02, initial_concentration=2.0, time_seconds=seconds, order=order
    ) == pytest.approx(0.75, abs=1e-12)


def test_a_pfr_is_a_batch_reactor_and_is_computed_as_one() -> None:
    """Identical, not merely close: `pfr_conversion` calls `batch_conversion`.

    Asserted with `==` on purpose. If somebody re-derives the PFR integral separately, this fails
    even though the two would agree to ten decimals — which is the point, because two copies of one
    equation is two places for it to be wrong in.
    """
    common = {"rate_constant": 0.01, "initial_concentration": 1.0}
    assert reactors.pfr_conversion(residence_time_seconds=90.0, **common) == (
        reactors.batch_conversion(time_seconds=90.0, **common)
    )


def test_the_second_order_cstr_bisection_matches_the_exact_quadratic_root() -> None:
    """The bisection is only trustworthy where a closed form exists to check it against.

    `τkC² + C - C₀ = 0` has the root `C = (-1 + √(1+4τkC₀))/(2τk)`, written here and nowhere in the
    module. Second order is the one non-trivial case with an exact answer, so it is the one that
    can audit the general solver.
    """
    rate, initial, residence = 0.05, 1.0, 40.0
    numeric = reactors.cstr_conversion(
        rate_constant=rate,
        initial_concentration=initial,
        residence_time_seconds=residence,
        order=2.0,
    )
    exact_outlet = (-1.0 + math.sqrt(1.0 + 4.0 * residence * rate * initial)) / (
        2.0 * residence * rate
    )
    assert numeric == pytest.approx(1.0 - exact_outlet / initial, abs=1e-9)


def test_a_cstr_always_converts_less_than_a_pfr_of_the_same_residence_time() -> None:
    """The inequality behind the ratio above, asserted across orders rather than at one point."""
    for order in (0.5, 1.0, 2.0):
        common = {
            "rate_constant": 0.02,
            "initial_concentration": 1.5,
            "residence_time_seconds": 60.0,
            "order": order,
        }
        assert reactors.cstr_conversion(**common) < reactors.pfr_conversion(**common)


def test_the_integrator_converges_at_fourth_order_which_is_what_caught_the_bug() -> None:
    """A rate, not a tolerance — and the assertion a loosened tolerance would have hidden.

    The first version of this module guarded the feed term with `time < dose_time_seconds`. That
    reads as defensive and is a defect: the last step's k4 stage evaluates at exactly
    `dose_time_seconds`, so it alone saw the feed switched off, putting one step of O(h) error into
    an O(h⁴) scheme. The symptom was that the error fell as 1/N rather than 1/N⁴ — which no
    absolute tolerance would have flagged, because the answer was still right to three figures and
    a test written to pass would have used four.

    So the assertion is on the *order*: refining by 2.5x must improve the answer by about 39.
    Under the defect it improved by 2.5.
    """
    reference = _peak(20_000)
    coarse = abs(_peak(200) - reference) / reference
    finer = abs(_peak(500) - reference) / reference
    improvement = coarse / finer
    assert improvement > 20.0, (
        f"refining 200 → 500 steps improved the answer by {improvement:.1f}x, where fourth-order "
        f"convergence gives about {2.5**4:.0f}x. A first-order rate here means a discontinuity "
        "inside the integration domain — check that no stage of the RK4 step sees a different "
        "feed rate from the others."
    )


def test_two_thousand_steps_is_enough_for_the_answer_this_server_returns() -> None:
    """The bound is sufficient, stated as the figure rather than as a belief."""
    assert abs(_peak(reactors.MAX_INTEGRATION_STEPS) - _peak(20_000)) / _peak(20_000) < 1e-9


def _peak(steps: int) -> float:
    """Peak accumulation for the worked case at a given step count.

    Raises the module bound for the reference grid only. A test that could not measure past the
    shipped default could not show the default is sufficient — it could only show it agrees with
    itself.
    """
    original = reactors.MAX_INTEGRATION_STEPS
    reactors.MAX_INTEGRATION_STEPS = max(original, steps)
    try:
        return _dose(steps=steps).peak_accumulation_fraction
    finally:
        reactors.MAX_INTEGRATION_STEPS = original


def test_an_instant_reaction_accumulates_nothing_and_an_inert_one_accumulates_everything() -> None:
    """The two limits the profile must hit, which bound every case between them.

    A reaction fast enough to consume the feed as it arrives leaves nothing to accumulate; one slow
    enough to be inert leaves the whole dose. Anything outside that range is a sign error.
    """
    instant = _dose(rate_constant=50.0)
    assert instant.peak_accumulation_fraction == pytest.approx(0.0, abs=1e-6)

    inert = _dose(rate_constant=1e-9)
    assert inert.peak_accumulation_fraction == pytest.approx(1.0, abs=1e-3)


def test_a_slower_dose_accumulates_less_which_is_the_whole_reason_to_dose_slowly() -> None:
    """The monotonicity a dose-time decision rests on, asserted rather than assumed."""
    peaks = [
        _dose(dose_time_seconds=seconds).peak_accumulation_fraction
        for seconds in (900.0, 3600.0, 7200.0, 21600.0)
    ]
    assert peaks == sorted(peaks, reverse=True), peaks


def test_the_profile_reports_the_peak_it_actually_found() -> None:
    """The summary fields must agree with the points they summarise, or the summary is fiction."""
    profile = _dose()
    highest = max(point.accumulated_fraction for point in profile.points)
    assert profile.peak_accumulation_fraction == highest
    at_peak = next(point for point in profile.points if point.accumulated_fraction == highest)
    assert profile.peak_at_seconds == at_peak.time_seconds
    assert profile.accumulation_at_end_of_dose == profile.points[-1].accumulated_fraction


def test_a_conversion_given_as_a_percentage_is_refused_with_the_mistake_named() -> None:
    """90 instead of 0.9 is the realistic error, and it is silently plausible to a parser."""
    with pytest.raises(KineticsInputError, match=r"0\.9, not 90"):
        reactors.time_for_batch_conversion(
            rate_constant=0.01, initial_concentration=1.0, conversion=90.0
        )


def test_a_conversion_past_where_a_rate_law_describes_anything_is_refused() -> None:
    """At 99.999% the time is set by mixing and impurities; the arithmetic would still answer."""
    with pytest.raises(KineticsInputError, match="beyond where an ideal rate law"):
        reactors.time_for_batch_conversion(
            rate_constant=0.01, initial_concentration=1.0, conversion=0.99999
        )


def test_a_negative_order_is_refused_rather_than_returning_a_rising_concentration() -> None:
    """Product inhibition is real; these integrated forms are not derived for it."""
    with pytest.raises(KineticsInputError, match="product "):
        reactors.batch_conversion(
            rate_constant=0.01, initial_concentration=1.0, time_seconds=60.0, order=-1.0
        )


def test_a_sub_first_order_reaction_reaches_completion_in_finite_time() -> None:
    """A real property of a zero-order rate law, reported as complete rather than raised.

    Zero order consumes at a constant rate regardless of concentration, so it genuinely runs out.
    Returning 1.0 is correct; raising would treat chemistry as a numerical failure.
    """
    assert (
        reactors.batch_conversion(
            rate_constant=1.0, initial_concentration=2.0, time_seconds=10.0, order=0.0
        )
        == 1.0
    )
