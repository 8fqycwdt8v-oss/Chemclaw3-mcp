"""Ideal isothermal reactors: batch, CSTR and PFR, plus semi-batch accumulation.

Three of the four are closed form. The fourth is integrated, in about thirty lines of Python, for
the reason `servers/thermalsafety` hand-rolled a bisection rather than import `scipy.optimize`: a
server is a dependency closure as much as a capability, and a fixed-step RK4 over two state
variables does not justify SciPy in this image.

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

from chemclaw_mcp_kinetics.engine.arrhenius import KineticsInputError

__all__ = [
    "DEFAULT_INTEGRATION_STEPS",
    "MAX_INTEGRATION_STEPS",
    "PROFILE_POINTS",
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

#: What a call actually runs at, and it is a *tenth* of the cap for a reason the bug fix bought.
#: While the feed-term discontinuity made convergence first-order, 2,000 steps was genuinely needed
#: to reach four figures — so that is what the default was set to, before anybody had measured the
#: rate. With the discontinuity gone, **200 steps agrees with a hundredfold finer grid to 6.4e-08**,
#: eight significant figures on a number reported to four, and costs 0.83 ms of CPU against 8.1 ms.
#:
#: That tenfold saving is also what keeps this server out of the band where a concurrency ceiling
#: is owed: at 8.1 ms it sat beside `chem`'s `render_structure`, the one tool in that server gated
#: for exactly this reason. A defect fixed made the control unnecessary rather than the control
#: covering for the defect.
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


def _positive(value: float, what: str) -> float:
    """A rate constant, a time or a concentration that must be above zero."""
    if value <= 0.0:
        raise KineticsInputError(f"{what} must be greater than zero; got {value}.")
    return value


def _order(value: float) -> float:
    """A reaction order, which may be fractional but not negative here.

    A negative order is real — an inhibiting product gives one — but the closed forms below are
    derived for `n >= 0` and would return a rising concentration for `n < 0` without saying so.
    """
    if value < 0.0:
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
    if time_seconds < 0.0:
        raise KineticsInputError(f"time must not be negative; got {time_seconds}.")
    if time_seconds == 0.0:
        return 0.0

    if math.isclose(order, 1.0):
        return 1.0 - math.exp(-rate_constant * time_seconds)

    exponent = 1.0 - order
    remaining_powered = initial_concentration**exponent + (order - 1.0) * rate_constant * (
        time_seconds
    )
    if remaining_powered <= 0.0:
        # For n < 1 the concentration reaches exactly zero in finite time — a real property of a
        # zero- or half-order rate law, not a numerical failure, so it is reported as complete
        # rather than raised.
        return 1.0
    remaining = float(remaining_powered ** (1.0 / exponent))
    return 1.0 - min(remaining / initial_concentration, 1.0)


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
        return -math.log(1.0 - conversion) / rate_constant

    remaining = initial_concentration * (1.0 - conversion)
    exponent = 1.0 - order
    return float(
        (remaining**exponent - initial_concentration**exponent) / ((order - 1.0) * rate_constant)
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
    if residence_time_seconds < 0.0:
        raise KineticsInputError(
            f"the residence time must not be negative; got {residence_time_seconds}."
        )
    if residence_time_seconds == 0.0:
        return 0.0

    if math.isclose(order, 1.0):
        product = rate_constant * residence_time_seconds
        return product / (1.0 + product)

    if math.isclose(order, 0.0):
        removed = rate_constant * residence_time_seconds
        return min(removed / initial_concentration, 1.0)

    # C₀ - C - τkCⁿ = 0 is strictly decreasing in C over (0, C₀]: at C = C₀ it is -τkC₀ⁿ < 0, and
    # as C → 0 it tends to C₀ > 0. So a bisection on (0, C₀] always brackets the root, with no
    # bracket-widening and no failure mode to report.
    def residual(concentration: float) -> float:
        return float(
            initial_concentration
            - concentration
            - residence_time_seconds * rate_constant * concentration**order
        )

    low, high = 0.0, initial_concentration
    for _ in range(200):
        middle = 0.5 * (low + high)
        if residual(middle) > 0.0:
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
    it at the concentrations' own bounds — `C_d <= dosed_moles / initial_volume` (every mole in the
    smallest volume) and `C_co <= C_co,0` (the co-reagent is only consumed and diluted). At
    `n_d = 1` the first bound drops out and this is the old `k * C_co^n_co` exactly.

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
    return float(
        rate_constant
        * order_in_dosed
        * max_dosed_concentration ** (order_in_dosed - 1.0)
        * initial_coreagent_concentration**order_in_coreagent
    )


def _steps_for_stability(
    *,
    stiffness: float,
    dose_time_seconds: float,
    requested: int,
) -> int:
    """The step count this dose actually needs, never fewer than `requested`.

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
    problem: a rate and a dose time somebody supplies cannot be covered by any fixed count. A call
    that would still be unstable at `MAX_INTEGRATION_STEPS` is refused by the caller rather than
    answered — a reaction that fast is mixing-limited, which is a regime this ideal model does not
    describe, and saying so is better than a zero.

    Args:
        stiffness: `_stiffness_bound`'s upper bound on the Jacobian's eigenvalue, per second.
        dose_time_seconds: How long the addition takes.
        requested: The caller's step count, which is a floor rather than the answer.

    Returns:
        The step count to integrate with — `requested` when the problem is not stiff.
    """
    if stiffness <= 0.0:
        return requested
    needed = math.ceil(dose_time_seconds * stiffness / RK4_REAL_STABILITY_LIMIT)
    return max(requested, needed)


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
) -> SemiBatchProfile:
    """Integrate a constant-rate semi-batch addition and report the accumulation profile.

    The question a dose time is chosen to answer: **how much unreacted dosed reagent is present at
    the worst moment?** That is the material a cooling failure would have to absorb all at once,
    and it is why "add it slowly" is a safety decision rather than a preference.

    Integrated with fixed-step RK4 over two states — moles of dosed reagent and moles of
    co-reagent — with the volume rising linearly as the dose goes in. No closed form exists: the
    volume and both concentrations move together.

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
    if dosed_volume < 0.0:
        raise KineticsInputError(f"the dosed volume must not be negative; got {dosed_volume}.")
    if not 1 <= steps <= MAX_INTEGRATION_STEPS:
        raise KineticsInputError(
            f"steps must be between 1 and {MAX_INTEGRATION_STEPS}; got {steps}. The upper bound is "
            "what this server prices."
        )
    # **Derived from the problem, because the old fixed count reported a fast reaction as a safe
    # one** — see `_steps_for_stability` for the measurement. `steps` is the caller's floor, not the
    # answer.
    stiffness = _stiffness_bound(
        rate_constant=rate_constant,
        max_dosed_concentration=dosed_moles / initial_volume,
        initial_coreagent_concentration=initial_coreagent_concentration,
        order_in_dosed=order_in_dosed,
        order_in_coreagent=order_in_coreagent,
    )
    steps = _steps_for_stability(
        stiffness=stiffness, dose_time_seconds=dose_time_seconds, requested=steps
    )
    if steps > MAX_INTEGRATION_STEPS:
        raise KineticsInputError(
            f"this dose is too fast for this integrator to resolve: the reaction's rate responds "
            f"to the concentrations at up to {stiffness:.3g} per second over the dose, which would "
            "need "
            f"{steps:,} steps over {dose_time_seconds:g} s to integrate stably and the ceiling is "
            f"{MAX_INTEGRATION_STEPS:,}. A reaction this fast relative to the addition is "
            "mixing-limited — the accumulation is set by how fast the feed disperses, not by the "
            "rate law — and that is a regime this ideal, perfectly-mixed model does not describe. "
            "Treat the accumulation as feed-rate-limited and size the dose from the heat-removal "
            "duty instead; `thermalsafety` is the server for that question. This refusal "
            "replaced a "
            "reported accumulation of zero, which read as a dose that is safe at any rate."
        )

    feed_rate = dosed_moles / dose_time_seconds
    volume_rate = dosed_volume / dose_time_seconds
    coreagent_moles_0 = initial_coreagent_concentration * initial_volume

    def volume_at(time: float) -> float:
        return initial_volume + volume_rate * min(time, dose_time_seconds)

    def _rate(dosed_c: float, coreagent_c: float) -> float:
        """The rate law, which is zero when either reagent is gone — whatever the orders.

        `max(c, 0.0) ** n` alone is not that: `0.0 ** 0.0` is 1, so a rate law of order zero in
        the dosed reagent kept consuming a reagent that was not there, and drove its own state
        negative.
        """
        if dosed_c <= 0.0 or coreagent_c <= 0.0:
            return 0.0
        return float(rate_constant * dosed_c**order_in_dosed * coreagent_c**order_in_coreagent)

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
    )
