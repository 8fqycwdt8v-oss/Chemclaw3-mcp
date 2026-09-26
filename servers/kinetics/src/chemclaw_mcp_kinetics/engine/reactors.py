"""Ideal isothermal reactors: batch, CSTR and PFR, plus semi-batch accumulation.

Three of the four are closed form. The fourth is integrated in plain Python, for the reason
`servers/thermalsafety` hand-rolled a bisection rather than import `scipy.optimize`: a server is a
dependency closure as much as a capability, and two fixed-step schemes over two state variables do
not justify SciPy in this image. Fixed-step RK4 answers every dose it can integrate stably within
`MAX_INTEGRATION_STEPS`; the stiff band past that — a reaction fast relative to its addition — is
integrated by an L-stable implicit scheme at a fixed step count instead of being refused
(`D-2026-09-26-a-stiff-dose-is-integrated-by-a-stable-scheme-not-refused`).

**What each reactor's equation assumes**, because the assumptions are what make a number wrong
rather than the arithmetic:

- *Batch* — perfectly mixed, constant volume, isothermal. `C(t)` follows from integrating
  `-dC/dt = k·Cⁿ`, which has a closed form for every order except that `n = 1` is the logarithmic
  case and every other `n` is the power case.
- *PFR* — **the same equation**, with residence time in place of clock time. A plug-flow reactor is
  a batch reactor moving down a pipe, and this module computes it that way rather than
  re-deriving it, which is also why they cannot disagree.
- *CSTR* — perfectly mixed and at steady state, so the whole reactor is at the *outlet*
  composition. That is why a CSTR needs more volume than a PFR for the same conversion, and the
  difference is not small: at 90% conversion in first order, `τ_CSTR/τ_PFR = 3.9`. A chemist
  moving a batch process to continuous meets this and is often surprised by it.
- *Semi-batch* — one reagent dosed at a constant rate into the other. Not closed form, because the
  volume and both concentrations change together; integrated instead.

**Isothermal, in every case.** Nothing here solves an energy balance. A reaction that heats its own
contents changes `k` as it runs, and that coupled problem is the one `servers/thermalsafety`
answers from calorimetry rather than the one this module answers from kinetics. Every result says
so.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from chemclaw_mcp_kinetics.engine.arrhenius import KineticsInputError, representable

__all__ = [
    "DEFAULT_INTEGRATION_STEPS",
    "MAX_INTEGRATION_STEPS",
    "METHOD_RK4",
    "METHOD_STABLE",
    "PROFILE_POINTS",
    "STABLE_INTEGRATION_STEPS",
    "AccumulationPoint",
    "SemiBatchProfile",
    "batch_conversion",
    "cstr_conversion",
    "pfr_conversion",
    "semibatch_accumulation",
    "time_for_batch_conversion",
]

#: The integrator's fixed step count. Chosen rather than adaptive because an adaptive controller is
#: what would want SciPy, and because the problem is smooth. A fixed count is also a bound on the
#: cost, which an adaptive scheme is not.
#:
#: **Measured, and the measurement found a defect rather than confirming a guess.** This comment
#: first claimed 2,000 steps agreed with a tenfold finer grid to 7 significant figures, written
#: from what RK4 ought to do. Driven, it agreed to *three* — and the error fell as 1/N, which is
#: first-order convergence from a fourth-order scheme and therefore a bug rather than a tolerance.
#: The cause was one line: a `time < dose_time_seconds` guard on the feed term meant the final
#: step's k4 stage alone saw the feed switched off. See `derivatives` below.
#:
#: With that removed, against a hundredfold finer grid on the worked case: 200 steps agrees to
#: 6.4e-08, 500 to 1.6e-09 and 2,000 to **6.2e-12** — and 200 → 500 improves by a factor of 40
#: against a step ratio of 2.5, which is 2.5^4, so the scheme converges at the order it claims.
#: The defect was not academic: it reported the peak accumulation **low**, 0.047856 against
#: 0.047940, and low is the dangerous direction for a number a dose time is chosen from.
MAX_INTEGRATION_STEPS = 200_000

#: RK4's stability limit on the real axis: the scheme is stable for `h*|lambda| <= 2.785` and
#: diverges above it. Classical, and the number this module needs rather than a tolerance somebody
#: chose — see `_steps_for_stability`, which is what stops a fast reaction being reported as a safe
#: one.
RK4_REAL_STABILITY_LIMIT = 2.785

#: How far above its quasi-steady value the dosed reagent's concentration is allowed for when the
#: step floor is derived. The quasi-steady value is itself an upper bound on what the dose reaches
#: (see `_dosed_concentration_bound`), so this is margin for RK4's transient and for the
#: co-reagent's partial derivative `_stiffness_bound` leaves out, not a correction. It enters the
#: stiffness as `margin^(n_d - 1)`: ten times the steps at second order, nothing at first order.
QUASI_STEADY_MARGIN = 10.0

#: What a call actually runs at, and it is a *tenth* of the cap for a reason the bug fix bought.
#: While the feed-term discontinuity made convergence first-order, 2,000 steps was genuinely needed
#: to reach four figures — so that is what the default was set to, before anybody had measured the
#: rate. With the discontinuity gone, **200 steps agrees with a hundredfold finer grid to 6.4e-08**,
#: eight significant figures on a number reported to four, and costs 0.83 ms of CPU against 8.1 ms.
#:
#: That tenfold saving was once the argument that this server owed no concurrency ceiling. It held
#: only while the count was fixed: `_steps_for_stability` below makes the cost the caller's, the
#: worst legal call is seconds, and `engine/admission.py` is the ceiling it now has.
#:
#: **This stays the default and is no longer the whole story, because "measured sufficient" was
#: measured on one case.** The 6.4e-08 agreement above was taken at `k = 0.02, C_co = 0.9`, i.e.
#: `h*lambda = 0.65` — comfortably inside RK4's stability region. The step count a stiff case needs
#: is not a constant, so `_steps_for_stability` derives a floor from the problem and
#: `MAX_INTEGRATION_STEPS` is now high enough to hold the realistic band. A call that would still be
#: unstable at the ceiling is refused rather than answered.
DEFAULT_INTEGRATION_STEPS = 200

#: The most points a `SemiBatchProfile` keeps: evenly spaced samples of the dose, its last instant,
#: and its peak. **The integrator's step count is not what bounds this, and it used to be.** Every
#: step was materialised as an `AccumulationPoint` and the tool thinned the list afterwards, so a
#: stiff dose at the step ceiling built ~200,000 objects to return 25 — memory proportional to a
#: number the *caller's* rate constant sets. The peak is tracked inside the loop instead, so memory
#: is O(this) and the peak is exact rather than whichever sample happened to land nearest it.
PROFILE_POINTS = 25

#: The two schemes a `SemiBatchProfile` can come out of, returned as its `method`.
METHOD_RK4 = "rk4"
METHOD_STABLE = "sdirk3-l-stable"

#: The stable scheme's step count, fixed rather than derived — which is the whole point of it: an
#: L-stable scheme is stable at every step size, so what sets the count is the accuracy the *slow*
#: part of the dose needs, not the reaction's speed. Its cost is therefore a constant however fast
#: the caller's reaction is, and that is what lets the stiff band be answered inside the admission
#: ceiling `engine/admission.py` derived for RK4's worst call.
#:
#: **Measured, against RK4 on the fixtures RK4 answers and against itself at 16,000 steps on the
#: ones it cannot.** At 2,000 steps the stable scheme agrees with RK4 to 7.9e-10 on the worked dose,
#: 2.6e-07 on the worked dose at `k = 50`, 4.5e-08 on the stiff dose at `k = 2.5` and 5.8e-08 on the
#: second-order dose; past the RK4 ceiling it agrees with a 465,000-step RK4 reference to 9.3e-08 on
#: the worked dose at `k = 200`, and with its own 16,000-step answer to 1.3e-09 at `k = 100` on the
#: stiff dose and to 2.7e-12 at `k = 1e12`. Third order on a smooth dose (500 -> 1,000 steps
#: improves 7.8x against 2^3 = 8); lower in the stiff band, where a stage order of one costs the
#: classical order, which is why the count is 2,000 and not the 200 RK4 needs.
STABLE_INTEGRATION_STEPS = 2_000

#: Backward-Euler sub-steps that replace the stable scheme's first step. **Without them the reported
#: peak was an artefact of the start**: the dose begins at zero accumulation, far off the
#: quasi-steady value the reaction pulls it to within `1/lambda` seconds, and the SDIRK stability
#: function is *negative* at large `h*lambda`, so the first step overshot that value and the
#: overshoot was the peak — measured 7.8e-03 high at `k = 200` against a zero-order co-reagent,
#: where the true profile rises monotonically to its plateau. Backward Euler's stability function
#: lies in (0, 1) for every `h*lambda`, so it can only approach from below; four of them damp the
#: start below 1e-9 and cost one step's worth of the O(h) error they carry (Rannacher's start, for
#: the same reason).
STABLE_START_SUBSTEPS = 4

# Alexander's three-stage, third-order, L-stable, stiffly accurate SDIRK (SIAM J. Numer. Anal. 14,
# 1977, table 5.2). Stiffly accurate means the last stage *is* the step's answer, so the step
# inherits the stage solve's guarantee that neither species goes below zero.
_SDIRK_GAMMA = 0.435866521508459
_SDIRK_TAU = (1.0 + _SDIRK_GAMMA) / 2.0
_SDIRK_B1 = -(6.0 * _SDIRK_GAMMA**2 - 16.0 * _SDIRK_GAMMA + 1.0) / 4.0
_SDIRK_B2 = (6.0 * _SDIRK_GAMMA**2 - 20.0 * _SDIRK_GAMMA + 5.0) / 4.0
_SDIRK_A: tuple[tuple[float, ...], ...] = (
    (),
    (_SDIRK_TAU - _SDIRK_GAMMA,),
    (_SDIRK_B1, _SDIRK_B2),
)
_SDIRK_C = (_SDIRK_GAMMA, _SDIRK_TAU, 1.0)

#: Newton iterations allowed per implicit stage before the bisection bracket alone must have closed.
#: Measured at three per stage across every fixture; the bracket halves on any iteration Newton
#: would leave, so this is a bound on the cost rather than a convergence tolerance.
_STAGE_ITERATIONS = 100

#: Conversions at or above this are refused as an input, because the time to reach them is where
#: the ideal-reactor idealisation stops describing anything: at 99.99% the answer is set by mixing,
#: impurities and the reverse reaction rather than by the rate law.
_MAX_MEANINGFUL_CONVERSION = 0.9999


def _fraction(value: float, what: str) -> float:
    """A conversion, which is a fraction between zero and one and not a percentage."""
    if not 0.0 <= value < 1.0:
        raise KineticsInputError(
            f"{what} must be a fraction at or above 0 and below 1; got {value}. 90% conversion is "
            "0.9, not 90 — and 1.0 exactly is unreachable in finite time for every order here."
        )
    if value > _MAX_MEANINGFUL_CONVERSION:
        raise KineticsInputError(
            f"{what} of {value} is beyond where an ideal rate law describes anything: at that "
            "level the time to reach it is set by mixing, trace impurities and the reverse "
            "reaction, not by the kinetics. Ask for 0.999 or below, or ask a different question."
        )
    return value


def _finite(value: float, what: str) -> float:
    """Any input to a rate law, which must be a real number before any other check means anything.

    `value <= 0.0` is False for NaN and for infinity alike, and pydantic's `gt=0` passes infinity,
    which the MCP JSON parser accepts as the literal `Infinity`. So an infinite rate constant used
    to reach `math.ceil` in the step floor and leave as an `OverflowError`, which `connector_app`
    replaces with an opaque `error_id` rather than a sentence the caller can act on.
    """
    if not math.isfinite(value):
        raise KineticsInputError(f"{what} must be a finite number; got {value}.")
    return value


def _positive(value: float, what: str) -> float:
    """A rate constant, a time or a concentration that must be finite and above zero."""
    if _finite(value, what) <= 0.0:
        raise KineticsInputError(f"{what} must be greater than zero; got {value}.")
    return value


def _order(value: float) -> float:
    """A reaction order, which may be fractional but not negative here.

    A negative order is real — an inhibiting product gives one — but the closed forms below are
    derived for `n >= 0` and would return a rising concentration for `n < 0` without saying so.
    """
    if _finite(value, "the reaction order") < 0.0:
        raise KineticsInputError(
            f"the reaction order must be zero or above; got {value}. A negative order (product "
            "inhibition) is real chemistry, and the integrated forms here are not derived for it: "
            "they would return a concentration that rises with time."
        )
    return value


def batch_conversion(
    *,
    rate_constant: float,
    initial_concentration: float,
    time_seconds: float,
    order: float = 1.0,
) -> float:
    """Fractional conversion in an isothermal batch reactor after a given time.

    Integrates `-dC/dt = k·Cⁿ`:

        n = 1 :  C = C₀·exp(-k·t)
        n ≠ 1 :  C^(1-n) = C₀^(1-n) + (n-1)·k·t

    Args:
        rate_constant: `k`, in the unit its order implies — s⁻¹ for first order,
            (concentration·s)⁻¹ for second, and so on. Not converted and not checked, because the
            unit is a consequence of `order` and the caller's concentration unit.
        initial_concentration: `C₀`, in the caller's own unit. Only its ratio to `C` matters for
            first order; for every other order the absolute value changes the answer, which is why
            it is required rather than defaulted to 1.
        time_seconds: How long the reaction runs.
        order: The order in the limiting reagent. 1.0 by default because it is by far the most
            common single-reagent case; 0 and 2 are the other two that appear routinely.

    Returns:
        Fractional conversion, between 0 and 1.

    Raises:
        KineticsInputError: If a value is not positive, or the order is negative.
    """
    _positive(rate_constant, "the rate constant")
    _positive(initial_concentration, "the initial concentration")
    _order(order)
    if not time_seconds >= 0.0:  # also true for NaN, which no branch below would refuse
        raise KineticsInputError(f"time must be zero or above; got {time_seconds}.")
    if time_seconds == 0.0:
        return 0.0

    if math.isclose(order, 1.0):
        return 1.0 - math.exp(-rate_constant * time_seconds)

    # Worked in ratio form, `(C/C₀)^(1-n) = 1 + (n-1)·a` with `a = k·t·C₀^(n-1)` the Damköhler
    # number, and `a` taken as a logarithm. **The absolute form `C^(1-n) = C₀^(1-n) + (n-1)·k·t`
    # refused inputs whose answer is an ordinary double**: `C₀^(1-n)` overflows at order 200 and
    # `C₀ = 1e-5` although the conversion is effectively zero, and `(n-1)·k·t` overflows at
    # `k·t` past DBL_MAX although the conversion is exactly one — at every order but the first,
    # which made the answer depend on the order. Here only the ratio is ever exponentiated, and a
    # ratio lies in [0, 1].
    shift = order - 1.0
    log_scaled = (
        math.log(abs(shift))
        + math.log(rate_constant)
        + math.log(time_seconds)
        + shift * math.log(initial_concentration)
    )  # log(|n-1|·a)
    if shift < 0.0:
        scaled = math.exp(min(log_scaled, 0.0))
        if scaled >= 1.0:
            # For n < 1 the concentration reaches exactly zero in finite time — a real property of
            # a zero- or half-order rate law, not a numerical failure, so it is reported as
            # complete rather than raised.
            return 1.0
        log_ratio = math.log1p(-scaled) / -shift
    else:
        # log(1 + (n-1)·a), without forming a term past DBL_MAX: past e^700 the 1 is invisible.
        log_state = log_scaled if log_scaled > 700.0 else math.log1p(math.exp(log_scaled))
        log_ratio = -log_state / shift
    return representable("the batch conversion", lambda: -math.expm1(log_ratio))


def time_for_batch_conversion(
    *,
    rate_constant: float,
    initial_concentration: float,
    conversion: float,
    order: float = 1.0,
) -> float:
    """How long an isothermal batch reactor needs to reach a given conversion, in seconds.

    The inverse of `batch_conversion`, in closed form rather than by search — which is why the two
    cannot drift apart at the fourth decimal the way a solver and its forward model can.

    Args:
        rate_constant: `k`, in the unit its order implies.
        initial_concentration: `C₀`, in the caller's own unit.
        conversion: The fractional conversion wanted, between 0 and 1.
        order: The order in the limiting reagent.

    Returns:
        The time in seconds.

    Raises:
        KineticsInputError: If a value is not positive, the order is negative, or the conversion is
            outside the range an ideal rate law describes.
    """
    _positive(rate_constant, "the rate constant")
    _positive(initial_concentration, "the initial concentration")
    _fraction(conversion, "the conversion")
    _order(order)
    if conversion == 0.0:
        return 0.0

    if math.isclose(order, 1.0):
        return representable("the batch time", lambda: -math.log(1.0 - conversion) / rate_constant)

    remaining = initial_concentration * (1.0 - conversion)
    exponent = 1.0 - order
    return representable(
        "the batch time",
        lambda: (
            (remaining**exponent - initial_concentration**exponent)
            / ((order - 1.0) * rate_constant)
        ),
    )


def pfr_conversion(
    *,
    rate_constant: float,
    initial_concentration: float,
    residence_time_seconds: float,
    order: float = 1.0,
) -> float:
    """Fractional conversion in an ideal plug-flow reactor.

    **This calls `batch_conversion` rather than re-deriving anything**, because an ideal PFR *is* a
    batch reactor with residence time in place of clock time. Writing the integrated forms twice
    would be two places for one equation to be wrong in.

    Args:
        rate_constant: `k`, in the unit its order implies.
        initial_concentration: `C₀` at the inlet, in the caller's own unit.
        residence_time_seconds: `τ = V/Q`, the reactor volume over the volumetric flow.
        order: The order in the limiting reagent.

    Returns:
        Fractional conversion at the outlet.

    Raises:
        KineticsInputError: As `batch_conversion`.
    """
    return batch_conversion(
        rate_constant=rate_constant,
        initial_concentration=initial_concentration,
        time_seconds=residence_time_seconds,
        order=order,
    )


def cstr_conversion(
    *,
    rate_constant: float,
    initial_concentration: float,
    residence_time_seconds: float,
    order: float = 1.0,
) -> float:
    """Fractional conversion in an ideal continuous stirred-tank reactor at steady state.

    The steady-state balance is algebraic rather than integrated, because a perfectly mixed tank is
    everywhere at its *outlet* composition:

        C₀ - C = τ·k·Cⁿ

    First order solves directly (`X = kτ/(1+kτ)`); second order is a quadratic; every other order
    is solved by bisection on a bracket that is guaranteed to contain the root, since the residual
    is monotonic in `C`.

    That outlet-composition fact is the whole reason a CSTR needs more volume than a PFR for the
    same conversion — the entire reactor runs at the *lowest* concentration in the system, so at
    the *slowest* rate.

    Args:
        rate_constant: `k`, in the unit its order implies.
        initial_concentration: `C₀` in the feed, in the caller's own unit.
        residence_time_seconds: `τ = V/Q`.
        order: The order in the limiting reagent.

    Returns:
        Fractional conversion at the outlet.

    Raises:
        KineticsInputError: If a value is not positive, or the order is negative.
    """
    _positive(rate_constant, "the rate constant")
    _positive(initial_concentration, "the initial concentration")
    _order(order)
    if not residence_time_seconds >= 0.0:  # also true for NaN, which the bisection would not see
        raise KineticsInputError(
            f"the residence time must be zero or above; got {residence_time_seconds}."
        )
    if residence_time_seconds == 0.0:
        return 0.0

    if math.isclose(order, 1.0):
        # `k·τ/(1+k·τ)` is `inf/inf` once `k·τ` passes DBL_MAX, and the answer there is 1.
        product = rate_constant * residence_time_seconds
        return product / (1.0 + product) if product <= 1.0 else 1.0 / (1.0 + 1.0 / product)

    if math.isclose(order, 0.0):
        removed = rate_constant * residence_time_seconds
        return min(removed / initial_concentration, 1.0)

    # C₀ - C - τkCⁿ = 0 is strictly decreasing in C over (0, C₀]: at C = C₀ it is -τkC₀ⁿ < 0, and
    # as C → 0 it tends to C₀ > 0. So a bisection on (0, C₀] always brackets the root, with no
    # bracket-widening and no failure mode to report.
    #
    # **Only the residual's sign is needed, and it is compared in logarithms**, because `τ·k·Cⁿ`
    # overflows a double at `C₀ = 1e120`, order 3, while the outlet it is bisecting for (~1e40) is
    # an ordinary number. Refusing the overflow refused a finite answer; an overflowing term only
    # ever means "consumption exceeds the gap", i.e. the root is below this `C`.
    log_tk = math.log(residence_time_seconds) + math.log(rate_constant)

    def feed_exceeds_consumption(concentration: float) -> bool:
        gap = initial_concentration - concentration
        if gap <= 0.0:
            return False
        if concentration <= 0.0:
            return True
        return math.log(gap) > log_tk + order * math.log(concentration)

    low, high = 0.0, initial_concentration
    for _ in range(200):
        middle = 0.5 * (low + high)
        if feed_exceeds_consumption(middle):
            low = middle
        else:
            high = middle
    outlet = 0.5 * (low + high)
    return 1.0 - min(outlet / initial_concentration, 1.0)


@dataclass(frozen=True)
class AccumulationPoint:
    """One instant in a semi-batch dose: what is in the vessel and how fast it is reacting."""

    time_seconds: float
    #: How much of the dosed reagent has been added so far, as a fraction of the whole dose.
    dosed_fraction: float
    #: The *unreacted* dosed reagent present, as a fraction of the whole dose. This is the number
    #: the question is about: it is the reagent that would still be there to react all at once if
    #: cooling failed at this instant.
    accumulated_fraction: float
    #: Concentration of the dosed reagent in the vessel, in the caller's own unit.
    concentration: float
    #: Instantaneous reaction rate, in concentration per second.
    rate: float


@dataclass(frozen=True)
class SemiBatchProfile:
    """A whole dose, and the worst instant in it."""

    #: At most `PROFILE_POINTS`: evenly spaced samples, the end of the dose, and the peak — never
    #: one point per integration step.
    points: tuple[AccumulationPoint, ...]
    #: The highest unreacted fraction reached at any point in the dose, and when. This is what a
    #: dose time is chosen to control: accumulation is the reagent a cooling failure would have to
    #: absorb, so the peak is the design case.
    peak_accumulation_fraction: float
    peak_at_seconds: float
    #: What is still unreacted when the addition finishes. A dose slow enough to keep accumulation
    #: low can still leave a tail, and the two are different questions.
    accumulation_at_end_of_dose: float
    #: `METHOD_RK4` or `METHOD_STABLE` — which scheme integrated this dose.
    method: str = METHOD_RK4
    #: How many steps it took. RK4's count is derived from the problem; the stable scheme's is
    #: `STABLE_INTEGRATION_STEPS` whatever the problem, which is what bounds its cost.
    steps: int = DEFAULT_INTEGRATION_STEPS
    #: `stiffness * dose_time`: how many times over the dose the reaction could consume what is in
    #: the vessel. Large is the regime where a perfectly-mixed model stops describing the vessel.
    dose_damkohler: float = 0.0


def _stiffness_bound(
    *,
    rate_constant: float,
    max_dosed_concentration: float,
    initial_coreagent_concentration: float,
    order_in_dosed: float,
    order_in_coreagent: float,
) -> float:
    """An upper bound, per second, on how fast the dosed reagent's own rate responds to it.

    That is `J_d = d(k*C_d^n_d*C_co^n_co)/dC_d = k*n_d*C_d^(n_d-1)*C_co^n_co`, bounded by evaluating
    it at the concentrations' own bounds — `C_d` at `_dosed_concentration_bound` and
    `C_co <= C_co,0` (the co-reagent is only consumed and diluted). At `n_d = 1` the first bound
    drops out and this is the old `k * C_co^n_co` exactly.

    **The order in the dosed reagent is in it, and it was not.** The bound used to be
    `k * C_co^n_co` whatever `n_d` was — right only at `n_d = 1`, and not even dimensionally a rate
    at `n_d = 2`, where it under-stepped a slow dose whose dosed reagent accumulates. An order below
    one contributes nothing here because its derivative is unbounded as the reagent is used up, and
    no step count covers that; `semibatch_accumulation` refuses that case by name when it happens.

    **The co-reagent's partial derivative is deliberately left out.** The Jacobian is rank one and
    its eigenvalue is `-(J_d + J_c)`, but `J_c` scales with `C_d`, which is small exactly when the
    reaction is fast. Bounding it by `C_d`'s ceiling measured as refusing the worked dose at
    `k = 50` — a case a fine grid answers at 3.22e-05 — for a stiffness it never reaches. The rare
    slow dose where `J_c` does matter is caught by the divergence check rather than answered.
    """
    if order_in_dosed < 1.0:
        return 0.0
    try:
        return float(
            rate_constant
            * order_in_dosed
            * max_dosed_concentration ** (order_in_dosed - 1.0)
            * initial_coreagent_concentration**order_in_coreagent
        )
    except OverflowError:
        # A float `**` raises rather than returning infinity; an unrepresentable stiffness is one
        # no step count covers, which `_steps_for_stability` refuses by name.
        return math.inf


def _dosed_concentration_bound(
    *,
    rate_constant: float,
    feed_rate: float,
    initial_volume: float,
    dosed_moles: float,
    initial_coreagent_concentration: float,
    order_in_dosed: float,
    order_in_coreagent: float,
) -> float:
    """The highest concentration of unreacted dosed reagent the step floor has to allow for.

    **Not `dosed_moles / initial_volume`, which it was.** That is the whole charge present
    unreacted in the starting volume — the one state a reacting dose never reaches — and at
    `n_d > 1` it enters the stiffness as `C_d^(n_d-1)`. Measured at `n_d = 2, k = 0.5` (1 h dose of
    40 mol into 1 against a co-reagent at 45), it put the floor at 2.3 million steps and refused as
    "too fast for this integrator" a dose that 5,000 steps answers to eleven figures (0.0024630155).
    A false refusal, in exactly the regime a dose time is chosen for.

    The bound used instead is the quasi-steady concentration, where consumption matches the feed:
    `C_d* = (F / (V * k * C_co^n_co))^(1/n_d)`. The dose starts at `C_d = 0` and cannot cross
    `C_d*(t)` from below — at it the net feed is zero and dilution only lowers `C_d` — and
    `V * C_d*` only grows as the volume rises and the co-reagent is used up, so `J_d = n_d*r/C_d` is
    largest at the start's quasi-steady value. Evaluated at `V_0` and `C_co,0` it therefore bounds
    `J_d` over the whole dose; `QUASI_STEADY_MARGIN` is headroom on top, and the total charge stays
    the ceiling for a slow reaction whose quasi-steady value the dose never reaches. The negative-
    mole check in `semibatch_accumulation` remains the backstop for anything this misses.

    At `n_d < 1` the result is unused (`_stiffness_bound` returns zero), and `1/n_d` is undefined at
    zero order, so the total charge is returned as-is.
    """
    total = dosed_moles / initial_volume
    if order_in_dosed < 1.0:
        return total
    try:
        quasi_steady = (
            feed_rate
            / (initial_volume * rate_constant * initial_coreagent_concentration**order_in_coreagent)
        ) ** (1.0 / order_in_dosed)
    except (OverflowError, ZeroDivisionError):
        return total
    return min(QUASI_STEADY_MARGIN * float(quasi_steady), total)


def _steps_for_stability(
    *,
    stiffness: float,
    dose_time_seconds: float,
    requested: int,
) -> int | None:
    """The RK4 step count this dose actually needs, never fewer than `requested` — or `None`.

    **A fixed step count reported a fast reaction as a perfectly safe one, and the clamps hid it.**
    At first order in each reagent the dosed-reagent ODE is pseudo-first-order with eigenvalue
    `lambda = k * C_co`, largest at `t = 0` where the co-reagent is undiluted; `_stiffness_bound`
    gives the general case. RK4 diverges once `h * lambda` passes
    `RK4_REAL_STABILITY_LIMIT`, and the divergence went *negative* — where `max(dosed, 0.0)` turned
    it into `accumulated_fraction = 0.0`, i.e. "no unreacted dosed reagent at any instant in the
    dose". That is the number `tools.semibatch_accumulation` calls "the material a cooling failure
    would have to absorb all at once", so the answer said the dose was safe at any rate.

    Measured on a 1 h dose of 5 mol into 0.10 volume against a co-reagent at 60, at the old fixed
    200
    steps:

    | `k` | `h*lambda` | reported peak | true peak |
    | --- | --- | --- | --- |
    | 0.005 | 5.4 | 0.00627642 | 0.00627636 |
    | 0.02 | 21.6 | 0.00043211 | 0.00163955 |
    | 0.05 | 54 | **0.00000000** | 0.00066222 |

    The regime that failed is the one a dose time is *chosen* for: a dose is slow **because** the
    reaction is fast. `steps` is not exposed on the tool, so a caller could not work around it.

    The floor is derived rather than raised to a new constant, because the number depends on the
    problem: a rate and a dose time somebody supplies cannot be covered by any fixed count.

    **Past `MAX_INTEGRATION_STEPS` this returns `None`, and the dose goes to the stable scheme.** It
    used to be refused there as mixing-limited — a refusal set by what an *explicit* scheme can
    afford, not by the chemistry, and
    `D-2026-09-26-a-stiff-dose-is-integrated-by-a-stable-scheme-not-refused` replaced it with an
    answer that carries that caveat instead. What is still refused
    is a stiffness too large to represent at all, which used to reach `math.ceil` and leave as an
    `OverflowError` the caller saw only as an `error_id`.

    Args:
        stiffness: `_stiffness_bound`'s upper bound on the Jacobian's eigenvalue, per second.
        dose_time_seconds: How long the addition takes.
        requested: The caller's step count, which is a floor rather than the answer.

    Returns:
        The RK4 step count — `requested` when the problem is not stiff — or `None` when no count up
        to `MAX_INTEGRATION_STEPS` integrates it stably.

    Raises:
        KineticsInputError: If the stiffness, or the step count it implies, is not a finite number.
    """
    if stiffness <= 0.0:
        return requested
    needed = dose_time_seconds * stiffness / RK4_REAL_STABILITY_LIMIT
    if not math.isfinite(needed):  # an infinite or NaN stiffness, or a product past DBL_MAX
        raise KineticsInputError(
            "this dose is too fast for any integrator here to represent: the rate's response to "
            "the concentrations over the dose is not a finite number in double precision. A "
            "reaction that fast relative to the addition is mixing-limited — the accumulation is "
            "set by how fast the feed disperses, not by the rate law — which this ideal, "
            "perfectly-mixed model does not describe. Check the units of the rate constant and "
            "the concentrations; if they are right, size the dose from the heat-removal duty "
            "instead (`thermalsafety`)."
        )
    if needed > MAX_INTEGRATION_STEPS:
        return None
    return max(requested, math.ceil(needed))


def semibatch_accumulation(
    *,
    rate_constant: float,
    dose_time_seconds: float,
    initial_volume: float,
    dosed_moles: float,
    dosed_volume: float,
    initial_coreagent_concentration: float,
    order_in_dosed: float = 1.0,
    order_in_coreagent: float = 1.0,
    steps: int = DEFAULT_INTEGRATION_STEPS,
    force_stable: bool = False,
) -> SemiBatchProfile:
    """Integrate a constant-rate semi-batch addition and report the accumulation profile.

    The question a dose time is chosen to answer: **how much unreacted dosed reagent is present at
    the worst moment?** That is the material a cooling failure would have to absorb all at once,
    and it is why "add it slowly" is a safety decision rather than a preference.

    Integrated over two states — moles of dosed reagent and moles of co-reagent — with the volume
    rising linearly as the dose goes in. No closed form exists: the volume and both concentrations
    move together. Fixed-step RK4 at a step count derived from the problem, or — when no count up to
    `MAX_INTEGRATION_STEPS` makes RK4 stable — Alexander's L-stable SDIRK at
    `STABLE_INTEGRATION_STEPS`. The profile's `method` says which.

    **Isothermal.** The vessel is assumed held at temperature, which is what a jacket is for and
    what makes this a kinetics question rather than a thermal one. If the jacket cannot hold it,
    the rate constant rises as the batch warms and the accumulation falls while the danger rises —
    that coupled problem is `servers/thermalsafety`'s, from calorimetry.

    Args:
        rate_constant: `k` for the reaction between the dosed reagent and the co-reagent, in the
            unit the two orders imply.
        dose_time_seconds: How long the addition takes, at a constant rate.
        initial_volume: The volume in the vessel before dosing, in the caller's own unit.
        dosed_moles: Total moles of the dosed reagent added over the whole addition.
        dosed_volume: The volume that dose occupies, in the same unit as `initial_volume`. Pass 0
            for a neat addition small enough to ignore.
        initial_coreagent_concentration: The co-reagent's concentration in the vessel at the start.
        order_in_dosed: Order in the dosed reagent.
        order_in_coreagent: Order in the co-reagent. Pass 0 for a large excess, which makes the
            reaction pseudo-first-order in the dosed reagent.
        steps: Integration steps. The default is measured sufficient; lowering it is for a test.
        force_stable: Integrate with the stable scheme even where RK4 would be stable. For the
            tests that hold the two schemes to each other; the tool never sets it.

    Returns:
        The profile, its peak accumulation and the value at the end of the dose.

    Raises:
        KineticsInputError: If a value is not positive, an order is negative, or `steps` is outside
            the range this server prices.
    """
    _positive(rate_constant, "the rate constant")
    _positive(dose_time_seconds, "the dose time")
    _positive(initial_volume, "the initial volume")
    _positive(dosed_moles, "the dosed moles")
    _positive(initial_coreagent_concentration, "the co-reagent concentration")
    _order(order_in_dosed)
    _order(order_in_coreagent)
    if _finite(dosed_volume, "the dosed volume") < 0.0:
        raise KineticsInputError(f"the dosed volume must not be negative; got {dosed_volume}.")
    if not 1 <= steps <= MAX_INTEGRATION_STEPS:
        raise KineticsInputError(
            f"steps must be between 1 and {MAX_INTEGRATION_STEPS}; got {steps}. The upper bound is "
            "what this server prices."
        )
    # **Derived from the problem, because the old fixed count reported a fast reaction as a safe
    # one** — see `_steps_for_stability` for the measurement. `steps` is the caller's floor, not the
    # answer.
    feed_rate = dosed_moles / dose_time_seconds
    stiffness = _stiffness_bound(
        rate_constant=rate_constant,
        max_dosed_concentration=_dosed_concentration_bound(
            rate_constant=rate_constant,
            feed_rate=feed_rate,
            initial_volume=initial_volume,
            dosed_moles=dosed_moles,
            initial_coreagent_concentration=initial_coreagent_concentration,
            order_in_dosed=order_in_dosed,
            order_in_coreagent=order_in_coreagent,
        ),
        initial_coreagent_concentration=initial_coreagent_concentration,
        order_in_dosed=order_in_dosed,
        order_in_coreagent=order_in_coreagent,
    )
    rk4_steps = _steps_for_stability(
        stiffness=stiffness, dose_time_seconds=dose_time_seconds, requested=steps
    )
    stable = force_stable or rk4_steps is None
    steps = max(steps, STABLE_INTEGRATION_STEPS) if rk4_steps is None or stable else rk4_steps
    volume_rate = dosed_volume / dose_time_seconds
    coreagent_moles_0 = initial_coreagent_concentration * initial_volume

    def volume_at(time: float) -> float:
        return initial_volume + volume_rate * min(time, dose_time_seconds)

    def _rate(dosed_c: float, coreagent_c: float) -> float:
        """The rate law, which is zero when either reagent is gone — whatever the orders.

        `max(c, 0.0) ** n` alone is not that: `0.0 ** 0.0` is 1, so a rate law of order zero in
        the dosed reagent kept consuming a reagent that was not there, and drove its own state
        negative.

        **A float `**` raises `OverflowError` rather than returning infinity**, and every guard
        upstream is about finiteness, not magnitude: at an order below one in the dosed reagent the
        stiffness bound is zero, so nothing priced the co-reagent's own power, and a finite
        `C_co = 1e200` at order 2 left here as an `OverflowError` that `connector_app` turns into an
        opaque `error_id`. Caught here, at the one expression that can raise, rather than by an
        arbitrary input ceiling: no magnitude bound on the inputs is physical, and a rate that
        cannot be represented is the refusal the caller needs to read.
        """
        if dosed_c <= 0.0 or coreagent_c <= 0.0:
            return 0.0
        try:
            return float(rate_constant * dosed_c**order_in_dosed * coreagent_c**order_in_coreagent)
        except OverflowError:
            raise KineticsInputError(
                f"the rate law overflows a double at a dosed-reagent concentration of "
                f"{dosed_c:.3g} and a co-reagent concentration of {coreagent_c:.3g} (orders "
                f"{order_in_dosed:g} and {order_in_coreagent:g}): no real solution is that "
                "concentrated. Check the units of the concentrations and of the rate constant."
            ) from None

    def derivatives(time: float, dosed: float, coreagent: float) -> tuple[float, float]:
        """d(moles)/dt for both species. Consumption is one-to-one in the dosed reagent."""
        volume = volume_at(time)
        consumed = _rate(dosed / volume, coreagent / volume) * volume
        # The feed is unconditional, because this integration's domain **is** the dose: t runs from
        # 0 to `dose_time_seconds` and no further. A `time < dose_time_seconds` guard here reads as
        # defensive and is a defect — the last step's k4 stage evaluates at exactly
        # `dose_time_seconds`, so it alone would see the feed switched off while k1-k3 saw it on.
        # That is one step of O(h) error inside an O(h^4) scheme, and it dominates: measured, it
        # dropped the integrator to *first-order* convergence, 2,000 steps agreeing with 20,000 to
        # three significant figures instead of the eleven RK4 gives without it.
        return feed_rate - consumed, -consumed

    def implicit_stage(
        time: float, dosed_known: float, coreagent_known: float, weight: float, guess: float
    ) -> tuple[float, float]:
        """Solve one implicit stage: `Y = known + weight * f(time, Y)`, both species at once.

        **Two unknowns reduce to one, exactly.** The feed enters only the dosed reagent and the
        consumption is one-to-one, so `Y_d - Y_c = known_d - known_c + weight * F` whatever the
        rate law — the stage is a scalar equation in `Y_d`. It is `Y_d + weight*R = target`, with
        `R >= 0` rising in `Y_d`, so its root is unique and bracketed: at or below `target`, and
        above the point where either species reaches zero (where `R` vanishes). Newton inside that
        bracket, bisecting whenever Newton would leave it, cannot diverge or go negative, which
        is the property an explicit step lacked.
        """
        offset = dosed_known - coreagent_known + weight * feed_rate  # Y_d - Y_c
        target = dosed_known + weight * feed_rate
        low, high = max(0.0, offset), target
        if high <= low:  # the rate is zero at the root: one species is already gone
            return high, high - offset
        dosed = min(max(guess, low), high)
        if dosed == low:
            dosed = high
        volume = volume_at(time)
        for _ in range(_STAGE_ITERATIONS):
            coreagent = dosed - offset
            consumed = _rate(dosed / volume, coreagent / volume) * volume
            residual = dosed - target + weight * consumed
            if residual > 0.0:
                high = dosed
            elif residual < 0.0:
                low = dosed
            else:
                break
            # d(consumed)/d(Y_d) along the stage line, where d(Y_c)/d(Y_d) = 1. Both species are
            # positive wherever `consumed` is, so neither division is by zero.
            slope = 1.0 + weight * consumed * (
                order_in_dosed / dosed + order_in_coreagent / coreagent
            )
            newton = dosed - residual / slope if math.isfinite(slope) else math.nan
            converged = low <= newton <= high and abs(newton - dosed) <= 1e-14 * newton
            dosed = newton if low <= newton <= high else 0.5 * (low + high)
            if converged or high - low <= 1e-15 * high:
                break
        return dosed, dosed - offset

    def stable_step(
        time: float, dosed: float, coreagent: float, first: bool
    ) -> tuple[float, float]:
        """One step of the stable scheme, from `time` to `time + step`.

        The first step is `STABLE_START_SUBSTEPS` backward-Euler sub-steps instead (see that
        constant). Every step ends by moving a state that truncation left below zero back onto the
        line `dosed - coreagent = F*t - n_co,0`, which every Runge-Kutta scheme conserves exactly: a
        co-reagent at -1e-6 mol means the reagent ran out inside the step, and the exact state
        there is zero co-reagent with that excess of dosed reagent. Without it a dose that uses its
        co-reagent up mid-way reported a peak 1.1e-04 low at 2,000 steps; with it, exactly the
        excess. **This is a projection onto the conserved line, not the clamp the divergence check
        forbids**: that clamp discarded moles, this keeps the difference the feed fixes.
        """
        if first:
            sub = step / STABLE_START_SUBSTEPS
            for index in range(1, STABLE_START_SUBSTEPS + 1):
                dosed, coreagent = implicit_stage(time + index * sub, dosed, coreagent, sub, dosed)
        else:
            gamma_step = _SDIRK_GAMMA * step
            slopes: list[tuple[float, float]] = []
            for weights, fraction in zip(_SDIRK_A, _SDIRK_C, strict=True):
                stage_time = time + fraction * step
                known_d = dosed + step * sum(w * k[0] for w, k in zip(weights, slopes, strict=True))
                known_c = coreagent + step * sum(
                    w * k[1] for w, k in zip(weights, slopes, strict=True)
                )
                stage_d, stage_c = implicit_stage(stage_time, known_d, known_c, gamma_step, dosed)
                slopes.append(derivatives(stage_time, stage_d, stage_c))
            dosed, coreagent = stage_d, stage_c  # stiffly accurate: the last stage is the answer
        if coreagent < 0.0:
            dosed, coreagent = dosed - coreagent, 0.0
        if dosed < 0.0:
            dosed, coreagent = 0.0, coreagent - dosed
        return dosed, coreagent

    # The profile is integrated over the dose itself. What happens after the addition stops is the
    # batch case, which `batch_conversion` answers and this does not duplicate.
    step = dose_time_seconds / steps
    dosed_moles_now, coreagent_now = 0.0, coreagent_moles_0

    def point_at(index: int, dosed: float, coreagent: float) -> AccumulationPoint:
        time = index * step
        volume = volume_at(time)
        return AccumulationPoint(
            time_seconds=time,
            dosed_fraction=min(feed_rate * time / dosed_moles, 1.0),
            accumulated_fraction=max(dosed, 0.0) / dosed_moles,
            concentration=max(dosed, 0.0) / volume,
            rate=_rate(dosed / volume, coreagent / volume),
        )

    # Evenly spaced samples, one slot short of `PROFILE_POINTS` so the peak always fits beside them.
    grid = PROFILE_POINTS - 1
    stride = steps / (grid - 1)
    sampled_indices = {round(sample * stride) for sample in range(grid)} | {steps}
    points: list[AccumulationPoint] = []
    peak_index, peak_moles, peak_coreagent = 0, 0.0, coreagent_moles_0

    # **A negative mole count is a diverged integration, not a physical zero, and clamping it was
    # what turned the divergence above into a reassuring answer.** The tolerance is relative to the
    # whole dose and generous: RK4 round-off on a state that is legitimately at zero is many orders
    # below it, so anything past it is the instability `_steps_for_stability` now prevents. Kept as
    # a belt rather than deleted, because an order below one has an unbounded Jacobian as its
    # reagent runs out, which no step floor covers — and that case gets its own message below, so a
    # reagent consumed as fast as it arrives is not misreported as a stiffness the floor missed.
    divergence_floor = -1e-9 * dosed_moles

    for index in range(steps + 1):
        time = index * step
        if (dosed_moles_now < divergence_floor or coreagent_now < divergence_floor) and (
            order_in_dosed < 1.0 or order_in_coreagent < 1.0
        ):
            raise KineticsInputError(
                f"a reagent ran out {time:g} s into the dose, and with a rate law of order "
                f"{order_in_dosed:g} in the dosed reagent and {order_in_coreagent:g} in the "
                "co-reagent this integrator cannot follow it there: below first order the rate "
                "does not fall to zero smoothly as a reagent is used up, so a fixed step "
                "overshoots into a negative mole count. What it means physically is that the "
                "reaction consumes that reagent as fast as it arrives — the accumulation is "
                "feed-rate-limited, not set by the rate law. Size the dose from the heat-removal "
                "duty instead (`thermalsafety`). This is refused rather than clamped to zero, "
                "because a clamped zero reads as a dose that is safe at any rate."
            )
        if dosed_moles_now < divergence_floor or coreagent_now < divergence_floor:
            raise KineticsInputError(
                f"the integration went unstable {time:g} s into the dose (dosed reagent "
                f"{dosed_moles_now:.3g} mol, co-reagent {coreagent_now:.3g} mol — a negative mole "
                f"count is not a physical state). This is a stiffness the step floor did not "
                f"catch; "
                "it is reported rather than clamped to zero, because a clamped zero reads as no "
                "accumulation at all and so as a dose that is safe at any rate."
            )
        if dosed_moles_now > peak_moles:
            peak_index, peak_moles, peak_coreagent = index, dosed_moles_now, coreagent_now
        if index in sampled_indices:
            points.append(point_at(index, dosed_moles_now, coreagent_now))
        if index == steps:
            break
        if stable:
            dosed_moles_now, coreagent_now = stable_step(
                time, dosed_moles_now, coreagent_now, index == 0
            )
            continue
        # Classical RK4 over the two-state system.
        k1a, k1b = derivatives(time, dosed_moles_now, coreagent_now)
        k2a, k2b = derivatives(
            time + step / 2, dosed_moles_now + step * k1a / 2, coreagent_now + step * k1b / 2
        )
        k3a, k3b = derivatives(
            time + step / 2, dosed_moles_now + step * k2a / 2, coreagent_now + step * k2b / 2
        )
        k4a, k4b = derivatives(
            time + step, dosed_moles_now + step * k3a, coreagent_now + step * k3b
        )
        dosed_moles_now += step * (k1a + 2 * k2a + 2 * k3a + k4a) / 6
        coreagent_now += step * (k1b + 2 * k2b + 2 * k3b + k4b) / 6

    peak = point_at(peak_index, peak_moles, peak_coreagent)
    if peak_index not in sampled_indices:
        points.append(peak)
        points.sort(key=lambda point: point.time_seconds)
    return SemiBatchProfile(
        points=tuple(points),
        peak_accumulation_fraction=peak.accumulated_fraction,
        peak_at_seconds=peak.time_seconds,
        accumulation_at_end_of_dose=points[-1].accumulated_fraction,
        method=METHOD_STABLE if stable else METHOD_RK4,
        steps=steps,
        dose_damkohler=stiffness * dose_time_seconds,
    )
