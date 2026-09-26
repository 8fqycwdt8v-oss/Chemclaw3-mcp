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

    **The fast arm asserted `approx(0.0, abs=1e-6)` at `k = 50`, and the physics contradicts it.**
    The true peak there is `3.22e-05` — thirty times the tolerance. The assertion passed because at
    the old fixed 200 steps `h*lambda` was 1,620, RK4 diverged, and `max(dosed, 0.0)` clamped the
    negative excursion to exactly zero. So the test was satisfied by the integrator failing, not by
    the limit: it would have passed just as well if the function had returned nothing at all, and at
    the same fixture's own `k = 1` (an entirely ordinary rate) the reported peak was `0.000408`
    against a true `0.001572` — 3.85x low, which is the dangerous direction for a number a dose time
    is chosen from.

    So the limit is asserted as the limit rather than as a zero: the peak *falls monotonically* as
    the reaction gets faster, and at `k = 50` it is the small number it actually is. `_dose` no
    longer needs a step count for this — `_steps_for_stability` derives one.
    """
    peaks = [_dose(rate_constant=k).peak_accumulation_fraction for k in (0.02, 1.0, 5.0, 50.0)]
    assert peaks == sorted(peaks, reverse=True), (
        f"a faster reaction accumulated more, which is a sign error: {peaks}"
    )
    assert peaks[-1] == pytest.approx(3.2205e-05, rel=1e-3), (
        "the fast limit is not the value an independent fine-grid integration gives; a zero "
        "here is "
        "the integrator diverging and being clamped, which reads as a dose that is safe at any rate"
    )
    assert peaks[-1] < peaks[0] / 1000, "the fast limit is not small relative to the slow one"

    inert = _dose(rate_constant=1e-9)
    assert inert.peak_accumulation_fraction == pytest.approx(1.0, abs=1e-3)


def test_a_dose_too_fast_to_integrate_is_refused_rather_than_reported_as_zero() -> None:
    """The complement of the limit above, and the reason it is a refusal and not a small number.

    Past `MAX_INTEGRATION_STEPS` the scheme cannot be made stable by taking more steps, and the old
    code's answer was `accumulated_fraction = 0.0` at every point, `peak_at_seconds = 0.0`, and a
    `basis` that said nothing was wrong — i.e. "no unreacted dosed reagent at any instant", which is
    the number `tools.semibatch_accumulation` calls the material a cooling failure would have to
    absorb. A reaction that fast relative to its addition is mixing-limited, which this ideal
    perfectly-mixed model does not describe, so the refusal says that and names the server that
    does.
    """
    with pytest.raises(KineticsInputError) as refused:
        _dose(rate_constant=200.0)

    message = str(refused.value)
    assert "mixing-limited" in message, f"the refusal does not say what regime this is: {message}"
    assert "thermalsafety" in message, "the refusal does not name where the question does belong"
    assert str(reactors.MAX_INTEGRATION_STEPS) in message.replace(",", ""), (
        "the refusal does not say what ceiling it hit"
    )


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


#: The stiff dose the reviews drove: a 1 h dose of 5 mol into 0.1 against a co-reagent at 60.
_STIFF_DOSE_SECONDS = 3600.0


def _stiff(*, rate_constant: float, order_in_dosed: float = 1.0) -> reactors.SemiBatchProfile:
    """The stiff case, named argument by argument for the reason `_dose` gives."""
    return reactors.semibatch_accumulation(
        rate_constant=rate_constant,
        dose_time_seconds=_STIFF_DOSE_SECONDS,
        initial_volume=0.1,
        dosed_moles=5.0,
        dosed_volume=0.0,
        initial_coreagent_concentration=60.0,
        order_in_dosed=order_in_dosed,
    )


def test_a_profile_keeps_a_bounded_set_of_points_whatever_the_step_count() -> None:
    """Memory is O(samples), not O(steps), and the peak is exact rather than the nearest sample.

    At `k = 2.5` the stability floor integrates ~194,000 steps; every one used to be materialised as
    an `AccumulationPoint` to return 25.
    """
    profile = _stiff(rate_constant=2.5)
    assert len(profile.points) <= reactors.PROFILE_POINTS
    assert profile.points[0].time_seconds == 0.0
    assert profile.points[-1].time_seconds == pytest.approx(_STIFF_DOSE_SECONDS)
    times = [point.time_seconds for point in profile.points]
    assert times == sorted(times)
    assert any(
        point.time_seconds == profile.peak_at_seconds
        and point.accumulated_fraction == profile.peak_accumulation_fraction
        for point in profile.points
    ), "the peak the summary reports is not among the points it summarises"


@pytest.mark.parametrize(("order_in_dosed", "rate_constant"), [(0.0, 0.05), (0.5, 0.5)])
def test_an_order_below_one_that_runs_its_reagent_out_says_so_rather_than_blaming_stiffness(
    order_in_dosed: float, rate_constant: float
) -> None:
    """Below first order the rate does not fall smoothly to zero, so no step floor covers it.

    Before the fix both cases were reported as "went unstable … a stiffness the step floor did not
    catch", which misstates the cause: the reaction consumes the dose as fast as it arrives.
    """
    with pytest.raises(KineticsInputError, match="ran out") as refused:
        _stiff(rate_constant=rate_constant, order_in_dosed=order_in_dosed)
    assert "feed-rate-limited" in str(refused.value)
    assert "stiffness" not in str(refused.value)


def test_a_slow_zero_order_dose_still_answers() -> None:
    """The refusal above is for a reagent that runs out, not for zero order as such."""
    profile = _stiff(rate_constant=1e-4, order_in_dosed=0.0)
    assert 0.0 < profile.peak_accumulation_fraction < 1.0


def test_the_stability_floor_carries_the_order_in_the_dosed_reagent() -> None:
    """At `n_d = 2` the bound is `k*2*C_d,max*C_co`, not the dimensionally wrong `k*C_co`."""
    bound = reactors._stiffness_bound(
        rate_constant=1e-3,
        max_dosed_concentration=50.0,
        initial_coreagent_concentration=60.0,
        order_in_dosed=2.0,
        order_in_coreagent=1.0,
    )
    assert bound == pytest.approx(1e-3 * 2.0 * 50.0 * 60.0)
    first_order = reactors._stiffness_bound(
        rate_constant=1e-3,
        max_dosed_concentration=50.0,
        initial_coreagent_concentration=60.0,
        order_in_dosed=1.0,
        order_in_coreagent=1.0,
    )
    assert first_order == pytest.approx(1e-3 * 60.0), "first order must be the old bound exactly"


def _second_order_dose(
    *,
    rate_constant: float,
    dosed_moles: float,
    initial_coreagent_concentration: float,
    dosed_volume: float,
    steps: int = reactors.DEFAULT_INTEGRATION_STEPS,
) -> reactors.SemiBatchProfile:
    """A 1 h dose into a volume of 1, second order in the dosed reagent and first in the other."""
    return reactors.semibatch_accumulation(
        rate_constant=rate_constant,
        dose_time_seconds=3600.0,
        initial_volume=1.0,
        dosed_moles=dosed_moles,
        dosed_volume=dosed_volume,
        initial_coreagent_concentration=initial_coreagent_concentration,
        order_in_dosed=2.0,
        order_in_coreagent=1.0,
        steps=steps,
    )


def test_a_second_order_dose_that_reacts_as_it_arrives_is_answered_rather_than_refused() -> None:
    """The step floor bounds `C_d` by what a dose reaches, not by the whole charge unreacted.

    Bounding it by `dosed_moles / initial_volume` put this dose at 2.3 million steps and refused it
    as "too fast for this integrator", while 5,000 steps answers it to eleven figures. The reference
    is a 20,000-step integration, which agrees with a 190,000-step one to 1e-15.
    """
    profile = _second_order_dose(
        rate_constant=0.5, dosed_moles=40.0, initial_coreagent_concentration=45.0, dosed_volume=0.5
    )
    assert profile.peak_accumulation_fraction == pytest.approx(0.0024630155, rel=1e-6)


def test_a_slow_second_order_dose_is_not_under_stepped(monkeypatch: pytest.MonkeyPatch) -> None:
    """The half the bound must still catch: a dose slow enough that its reagent accumulates.

    Here the co-reagent runs out halfway, so half the charge is left unreacted — exactly, which is
    the reference. `J_d = 2*k*C_d*C_co` is then far above `k*C_co`, and the bound that ignored the
    order in the dosed reagent took 200 steps and diverged. The second half pins that direction, so
    the first half is evidence that the floor is doing the work rather than the default count.
    """
    profile = _second_order_dose(
        rate_constant=1e-3, dosed_moles=200.0, initial_coreagent_concentration=100.0, dosed_volume=0
    )
    assert profile.peak_accumulation_fraction == pytest.approx(0.5, rel=1e-6)

    def order_blind(**bound: float) -> float:
        return bound["rate_constant"] * bound["initial_coreagent_concentration"]

    monkeypatch.setattr(reactors, "_stiffness_bound", order_blind)
    with pytest.raises(KineticsInputError, match="went unstable"):
        _second_order_dose(
            rate_constant=1e-3,
            dosed_moles=200.0,
            initial_coreagent_concentration=100.0,
            dosed_volume=0,
        )


_FLOAT_ARGUMENTS = (
    "rate_constant",
    "dose_time_seconds",
    "initial_volume",
    "dosed_moles",
    "dosed_volume",
    "initial_coreagent_concentration",
    "order_in_dosed",
    "order_in_coreagent",
)


@pytest.mark.parametrize("bad", [math.inf, math.nan])
@pytest.mark.parametrize("argument", _FLOAT_ARGUMENTS)
def test_a_non_finite_dose_input_is_refused_by_name(argument: str, bad: float) -> None:
    """Infinity passes `gt=0` and `value <= 0.0`, and used to leave `math.ceil` as an overflow.

    `connector_app` replaces an `OverflowError` with an opaque `error_id`; a `KineticsInputError`
    reaches the caller as a sentence.
    """
    values = {
        "rate_constant": 0.02,
        "dose_time_seconds": 3600.0,
        "initial_volume": 1.0,
        "dosed_moles": 40.0,
        "dosed_volume": 0.5,
        "initial_coreagent_concentration": 45.0,
        "order_in_dosed": 2.0,
        "order_in_coreagent": 1.0,
    }
    values[argument] = bad
    with pytest.raises(KineticsInputError, match="finite"):
        reactors.semibatch_accumulation(
            rate_constant=values["rate_constant"],
            dose_time_seconds=values["dose_time_seconds"],
            initial_volume=values["initial_volume"],
            dosed_moles=values["dosed_moles"],
            dosed_volume=values["dosed_volume"],
            initial_coreagent_concentration=values["initial_coreagent_concentration"],
            order_in_dosed=values["order_in_dosed"],
            order_in_coreagent=values["order_in_coreagent"],
        )


def test_a_stiffness_too_large_to_represent_is_the_too_fast_refusal() -> None:
    """Finite inputs whose bound overflows a float are refused as too fast, not as an overflow."""
    with pytest.raises(KineticsInputError, match="too fast"):
        reactors.semibatch_accumulation(
            rate_constant=1e300,
            dose_time_seconds=3600.0,
            initial_volume=1.0,
            dosed_moles=1.0,
            dosed_volume=0.0,
            initial_coreagent_concentration=1e200,
            order_in_dosed=1.0,
            order_in_coreagent=2.0,
        )


@pytest.mark.parametrize("order_in_dosed", [0.0, 0.5])
def test_a_rate_law_too_large_to_represent_is_refused_by_name(order_in_dosed: float) -> None:
    """Below first order in the dosed reagent the stiffness bound is zero, so nothing priced
    `C_co ** n_co` before the integrator evaluated it — and a finite `1e200` squared left the rate
    law as an `OverflowError`, which `connector_app` replaces with an opaque `error_id`.
    """
    with pytest.raises(KineticsInputError, match="overflows"):
        reactors.semibatch_accumulation(
            rate_constant=1e-3,
            dose_time_seconds=3600.0,
            initial_volume=1.0,
            dosed_moles=1.0,
            dosed_volume=0.0,
            initial_coreagent_concentration=1e200,
            order_in_dosed=order_in_dosed,
            order_in_coreagent=2.0,
        )
