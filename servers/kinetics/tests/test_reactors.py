"""The reactors, checked against relations that hold independently of this code.

A textbook ratio, an exact quadratic root, an inverse that must round-trip, and a convergence
order, none of which the implementation contains.
"""

from __future__ import annotations

import math
from collections.abc import Callable

import pytest
from chemclaw_mcp_kinetics.engine import reactors
from chemclaw_mcp_kinetics.engine.arrhenius import KineticsInputError

#: The worked semi-batch case used throughout: an acid chloride dosed into an amine over two hours.
#: Passed to `_dose` by keyword, because a float-valued `**` splat cannot type-check against the
#: signature's one `int` parameter.
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

    `τ_CSTR/τ_PFR = X/((1-X)·ln(1/(1-X)))`, 3.909 at X = 0.9: the whole tank sits at the outlet
    composition, so at the slowest rate.
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

    `==` on purpose, so a separately re-derived PFR integral fails even if it agrees numerically.
    """
    common = {"rate_constant": 0.01, "initial_concentration": 1.0}
    assert reactors.pfr_conversion(residence_time_seconds=90.0, **common) == (
        reactors.batch_conversion(time_seconds=90.0, **common)
    )


def test_the_second_order_cstr_bisection_matches_the_exact_quadratic_root() -> None:
    """The second-order CSTR bisection matches the exact quadratic root.

    `τkC² + C - C₀ = 0` has the root `C = (-1 + √(1+4τkC₀))/(2τk)`, written only here; the one
    non-trivial case with a closed form audits the general solver.
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
    """The integrator converges at fourth order: a rate, not a tolerance.

    A feed term switched off at exactly `dose_time_seconds` would put O(h) error into the last RK4
    stage, an error a tolerance would not flag. Refining by 2.5x must improve the answer by about
    2.5^4 = 39.
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

    Raises the module bound for the reference grid only, so the shipped default can be shown
    sufficient rather than self-consistent.
    """
    original = reactors.MAX_INTEGRATION_STEPS
    reactors.MAX_INTEGRATION_STEPS = max(original, steps)
    try:
        return _dose(steps=steps).peak_accumulation_fraction
    finally:
        reactors.MAX_INTEGRATION_STEPS = original


def test_an_instant_reaction_accumulates_nothing_and_an_inert_one_accumulates_everything() -> None:
    """An instant reaction accumulates almost nothing and an inert one accumulates everything.

    These limits bound every case; anything outside them is a sign error. The fast limit is asserted
    as a monotone fall to its true small value, not as an exact zero, because a clamped divergent
    integrator would also return zero.
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


def test_a_dose_past_the_explicit_ceiling_is_answered_by_the_stable_scheme() -> None:
    """A dose past the explicit-scheme step ceiling is answered by the stable scheme.

    The reference is RK4 with the ceiling lifted; the stable scheme's fixed steps match it. The
    mixing-limited warning is kept as the answer's caveat.
    """
    profile = _dose(rate_constant=200.0)
    assert profile.method == reactors.METHOD_STABLE
    assert profile.steps == reactors.STABLE_INTEGRATION_STEPS
    assert profile.peak_accumulation_fraction == pytest.approx(8.0545086724e-06, rel=1e-6)
    assert profile.dose_damkohler * reactors.RK4_REAL_STABILITY_LIMIT > (
        reactors.RK4_REAL_STABILITY_LIMIT * reactors.MAX_INTEGRATION_STEPS
    ), "the fixture is no longer past the RK4 ceiling, so this test no longer tests the switch"


def test_a_dose_rk4_can_integrate_is_still_integrated_by_rk4() -> None:
    """The switch is at the ceiling and nowhere else, so every answer RK4 gave is unchanged."""
    for rate_constant in (0.02, 50.0):
        assert _dose(rate_constant=rate_constant).method == reactors.METHOD_RK4
    assert _stiff(rate_constant=2.5).method == reactors.METHOD_RK4
    assert _stiff(rate_constant=2.6).method == reactors.METHOD_STABLE


def _both(
    *,
    rate_constant: float,
    dose_time_seconds: float,
    initial_volume: float,
    dosed_moles: float,
    dosed_volume: float,
    initial_coreagent_concentration: float,
    order_in_dosed: float,
) -> tuple[float, float]:
    """The peak by the scheme the problem picks, and by the stable scheme forced."""
    common = {
        "rate_constant": rate_constant,
        "dose_time_seconds": dose_time_seconds,
        "initial_volume": initial_volume,
        "dosed_moles": dosed_moles,
        "dosed_volume": dosed_volume,
        "initial_coreagent_concentration": initial_coreagent_concentration,
        "order_in_dosed": order_in_dosed,
    }
    auto = reactors.semibatch_accumulation(
        steps=reactors.DEFAULT_INTEGRATION_STEPS, force_stable=False, **common
    )
    assert auto.method == reactors.METHOD_RK4, "a regression fixture must be one RK4 answers"
    stable = reactors.semibatch_accumulation(
        steps=reactors.DEFAULT_INTEGRATION_STEPS, force_stable=True, **common
    )
    assert stable.method == reactors.METHOD_STABLE
    return auto.peak_accumulation_fraction, stable.peak_accumulation_fraction


@pytest.mark.parametrize(
    ("rate_constant", "dose_time_seconds", "initial_volume", "dosed_moles", "dosed_volume",
     "initial_coreagent_concentration", "order_in_dosed"),
    [
        pytest.param(0.02, 7200.0, 50.0, 40.0, 8.0, 0.9, 1.0, id="worked"),
        pytest.param(50.0, 7200.0, 50.0, 40.0, 8.0, 0.9, 1.0, id="worked-k50"),
        pytest.param(0.02, 3600.0, 0.1, 5.0, 0.0, 60.0, 1.0, id="stiff-k0.02"),
        pytest.param(2.5, 3600.0, 0.1, 5.0, 0.0, 60.0, 1.0, id="stiff-k2.5"),
        pytest.param(0.5, 3600.0, 1.0, 40.0, 0.5, 45.0, 2.0, id="second-order"),
        pytest.param(1e-3, 3600.0, 1.0, 200.0, 0.0, 100.0, 2.0, id="second-order-slow"),
    ],
)  # fmt: skip
def test_the_stable_scheme_agrees_with_rk4_on_every_fixture_rk4_answers(
    rate_constant: float,
    dose_time_seconds: float,
    initial_volume: float,
    dosed_moles: float,
    dosed_volume: float,
    initial_coreagent_concentration: float,
    order_in_dosed: float,
) -> None:
    """The stable scheme agrees with RK4 on every fixture RK4 answers.

    The tolerance is tight enough that a mistyped SDIRK coefficient would miss by orders of
    magnitude.
    """
    rk4, stable = _both(
        rate_constant=rate_constant,
        dose_time_seconds=dose_time_seconds,
        initial_volume=initial_volume,
        dosed_moles=dosed_moles,
        dosed_volume=dosed_volume,
        initial_coreagent_concentration=initial_coreagent_concentration,
        order_in_dosed=order_in_dosed,
    )
    assert stable == pytest.approx(rk4, rel=3e-6)


def test_the_stable_scheme_converges_at_third_order_on_a_smooth_dose() -> None:
    """The stable scheme converges at third order on a smooth dose.

    Alexander's SDIRK: doubling the steps improves the answer by about 8; the floor of 5 catches a
    scheme that lost an order.
    """
    reference = _peak(20_000)

    def error(steps: int) -> float:
        stable = reactors.semibatch_accumulation(
            rate_constant=_RATE_CONSTANT,
            dose_time_seconds=_DOSE_TIME_SECONDS,
            initial_volume=_INITIAL_VOLUME,
            dosed_moles=_DOSED_MOLES,
            dosed_volume=_DOSED_VOLUME,
            initial_coreagent_concentration=_COREAGENT_CONCENTRATION,
            steps=steps,
            force_stable=True,
        )
        return abs(stable.peak_accumulation_fraction - reference) / reference

    improvement = error(2_000) / error(4_000)
    assert improvement > 5.0, f"doubling the steps improved the answer {improvement:.1f}x, not ~8x"


@pytest.mark.parametrize("rate_constant", [1e2, 1e6, 1e12])
def test_a_dose_that_reacts_as_it_arrives_lands_on_the_quasi_steady_limit(
    rate_constant: float,
) -> None:
    """Far past the RK4 ceiling the answer has a closed form the scheme does not contain.

    The feed reacts as it arrives, so the peak unreacted fraction at the end of the dose is
    `1 / (k * C_co,end * t_dose)`; on the stiff fixture `C_co,end` is (6 - 5) mol / 0.1 = 10.
    """
    profile = _stiff(rate_constant=rate_constant)
    assert profile.method == reactors.METHOD_STABLE
    limit = 1.0 / (rate_constant * 10.0 * _STIFF_DOSE_SECONDS)
    assert profile.peak_accumulation_fraction == pytest.approx(limit, rel=1e-5)
    assert profile.peak_at_seconds == pytest.approx(_STIFF_DOSE_SECONDS)


@pytest.mark.parametrize(
    ("rate_constant", "order_in_coreagent"), [(1e2, 1.0), (1e6, 1.0), (1e6, 0.5)]
)
def test_a_co_reagent_used_up_mid_dose_leaves_exactly_the_excess(
    rate_constant: float, order_in_coreagent: float
) -> None:
    """A fast reaction that runs out of co-reagent halfway leaves half the charge, exactly.

    The state is projected back onto the conserved line `n_d - n_co`, so truncation within a step
    cannot drive the co-reagent negative.
    """
    profile = reactors.semibatch_accumulation(
        rate_constant=rate_constant,
        dose_time_seconds=3600.0,
        initial_volume=1.0,
        dosed_moles=200.0,
        dosed_volume=2.0,
        initial_coreagent_concentration=100.0,
        order_in_coreagent=order_in_coreagent,
    )
    assert profile.method == reactors.METHOD_STABLE
    assert profile.peak_accumulation_fraction == pytest.approx(0.5, rel=1e-9)
    assert all(point.accumulated_fraction >= 0.0 for point in profile.points)


def test_the_start_of_a_stiff_dose_does_not_overshoot_into_a_false_peak() -> None:
    """With a zero-order co-reagent the true profile rises monotonically to `F/k` and stays there.

    SDIRK's stability function is negative at large `h*lambda`, so a first step from zero would
    overshoot into a false peak; the backward-Euler start approaches from below.
    """
    rate_constant = 200.0
    profile = reactors.semibatch_accumulation(
        rate_constant=rate_constant,
        dose_time_seconds=3600.0,
        initial_volume=1.0,
        dosed_moles=40.0,
        dosed_volume=0.5,
        initial_coreagent_concentration=45.0,
        order_in_coreagent=0.0,
    )
    assert profile.method == reactors.METHOD_STABLE
    plateau = 1.0 / (rate_constant * 3600.0)
    assert profile.peak_accumulation_fraction <= plateau * (1.0 + 1e-9)
    assert profile.peak_accumulation_fraction == pytest.approx(plateau, rel=1e-9)


def test_the_stable_scheme_costs_a_fixed_step_count_however_fast_the_reaction() -> None:
    """What bounds its cost inside the admission ceiling: the count does not follow the rate."""
    counts = {_stiff(rate_constant=k).steps for k in (3.0, 1e3, 1e9, 1e15)}
    assert counts == {reactors.STABLE_INTEGRATION_STEPS}
    assert reactors.STABLE_INTEGRATION_STEPS * 100 <= reactors.MAX_INTEGRATION_STEPS, (
        "the stable scheme does three implicit stages of about three Newton iterations each per "
        "step, ~30x an RK4 step's work; at a hundredth of RK4's step ceiling its worst call stays "
        "a small fraction of the one `engine/admission.py` sized the ceiling for"
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

    At `k = 2.5` the stability floor integrates ~194,000 steps, and only the returned samples are
    kept.
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

    The refusal says the reaction consumes the dose as fast as it arrives, rather than blaming
    stiffness.
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

    The loose bound would refuse a dose a few thousand steps answer; the reference is a finer
    integration.
    """
    profile = _second_order_dose(
        rate_constant=0.5, dosed_moles=40.0, initial_coreagent_concentration=45.0, dosed_volume=0.5
    )
    assert profile.peak_accumulation_fraction == pytest.approx(0.0024630155, rel=1e-6)


def test_a_slow_second_order_dose_is_not_under_stepped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow second-order dose is not under-stepped.

    The co-reagent runs out halfway, so half the charge is the exact reference, and the stiffness
    `J_d = 2*k*C_d*C_co` is far above `k*C_co`. This direction proves the floor, not the default
    step count, does the work.
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
    """Infinity passes `gt=0` and `value <= 0.0`, so it is refused explicitly.

    An `OverflowError` would reach the caller as an opaque `error_id`; a `KineticsInputError`
    reaches it as a sentence.
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


@pytest.mark.parametrize("order", [0.0, 0.5, 2.0])
def test_a_rate_constant_times_time_past_dbl_max_is_complete_conversion_at_every_order(
    order: float,
) -> None:
    """`(n-1)·k·t` past DBL_MAX is complete conversion at every order, not an overflow.

    The outcome must not depend on the order; the exact answer is an ordinary double.
    """
    assert reactors.batch_conversion(
        rate_constant=1e200, initial_concentration=1.0, time_seconds=1e200, order=order
    ) == pytest.approx(1.0)
    assert reactors.batch_conversion(
        rate_constant=1e200, initial_concentration=1.0, time_seconds=1e200, order=1.0
    ) == pytest.approx(1.0)


def test_a_pfr_whose_damkohler_number_overflows_is_complete_conversion() -> None:
    """`pfr_conversion` delegates to `batch_conversion`, so it inherited the same false refusal."""
    assert reactors.pfr_conversion(
        rate_constant=1e300, initial_concentration=1.0, residence_time_seconds=1e10, order=2.0
    ) == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        pytest.param(
            lambda: reactors.cstr_conversion(
                rate_constant=1.0,
                initial_concentration=1e120,
                residence_time_seconds=1.0,
                order=3.0,
            ),
            1.0 - 1e40 / 1e120,
            id="cstr-power-past-dbl-max",
        ),
        pytest.param(
            lambda: reactors.cstr_conversion(
                rate_constant=1.0,
                initial_concentration=1e200,
                residence_time_seconds=1.0,
                order=3.0,
            ),
            1.0,
            id="cstr-power-far-past-dbl-max",
        ),
        pytest.param(
            lambda: reactors.cstr_conversion(
                rate_constant=1e200,
                initial_concentration=1.0,
                residence_time_seconds=1e200,
                order=1.0,
            ),
            1.0,
            id="cstr-first-order-inf-over-inf",
        ),
        pytest.param(
            lambda: reactors.batch_conversion(
                rate_constant=1.0, initial_concentration=1e-5, time_seconds=1.0, order=200.0
            ),
            0.0,
            id="batch-c0-power-past-dbl-max",
        ),
        pytest.param(
            lambda: reactors.pfr_conversion(
                rate_constant=1.0,
                initial_concentration=1e-5,
                residence_time_seconds=1.0,
                order=200.0,
            ),
            0.0,
            id="pfr-c0-power-past-dbl-max",
        ),
    ],
)
def test_an_intermediate_past_dbl_max_does_not_refuse_a_representable_answer(
    call: Callable[[], float], expected: float
) -> None:
    """An intermediate past DBL_MAX does not refuse a representable answer.

    E.g. at order 200 the `C₀^(1-n)` term overflows while conversion is effectively zero; a CSTR's
    bisection may overflow while the outlet is finite.
    """
    assert call() == pytest.approx(expected, abs=1e-12)


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda: reactors.time_for_batch_conversion(
                rate_constant=1.0, initial_concentration=1e-5, conversion=0.5, order=200.0
            ),
            id="batch-time",
        ),
        pytest.param(
            lambda: reactors.time_for_batch_conversion(
                rate_constant=1e-320, initial_concentration=1.0, conversion=0.5, order=1.0
            ),
            id="batch-time-first-order-inf",
        ),
    ],
)
def test_a_closed_form_that_overflows_is_refused_by_name(call: Callable[[], float]) -> None:
    """A closed form that genuinely overflows is refused by name.

    An `OverflowError` would reach the model as an opaque `error_id`, and `inf`/`nan` must not be
    returned as answers. Only results really past DBL_MAX belong here.
    """
    with pytest.raises(KineticsInputError, match="overflows a double"):
        call()


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda: reactors.batch_conversion(
                rate_constant=1.0, initial_concentration=1.0, time_seconds=math.nan, order=2.0
            ),
            id="batch",
        ),
        pytest.param(
            lambda: reactors.cstr_conversion(
                rate_constant=1.0,
                initial_concentration=1.0,
                residence_time_seconds=math.nan,
                order=2.0,
            ),
            id="cstr",
        ),
    ],
)
def test_a_nan_time_is_refused_rather_than_answered(call: Callable[[], float]) -> None:
    """`NaN < 0` is False, and the CSTR's log-space bisection would read a NaN comparison as "the
    root is lower" all the way down to complete conversion — so the check is `not t >= 0`.
    """
    with pytest.raises(KineticsInputError, match="zero or above"):
        call()
