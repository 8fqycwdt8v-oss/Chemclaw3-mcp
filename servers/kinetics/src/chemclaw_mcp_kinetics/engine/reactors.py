"""Ideal isothermal reactors: batch, CSTR and PFR, plus semi-batch accumulation.

- *Batch*: perfectly mixed, constant volume; `-dC/dt = k·Cⁿ` in closed form.
- *PFR*: the batch equation with residence time for clock time, computed by calling it.
- *CSTR*: steady state, the whole tank at outlet composition — hence more volume than a PFR.
- *Semi-batch*: one reagent dosed at constant rate; integrated, since volume and both
  concentrations change together. Fixed-step RK4 where stable within `MAX_INTEGRATION_STEPS`,
  otherwise an L-stable SDIRK at a fixed step count.

Plain Python, so the image carries no SciPy. Isothermal in every case: nothing here solves an
energy balance (that is `servers/thermalsafety`'s), and every result says so.
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

#: Ceiling on RK4 steps. Fixed-step rather than adaptive keeps SciPy out and bounds the cost; the
#: feed term must stay unconditional (see `derivatives`) for the scheme to converge at fourth order.
MAX_INTEGRATION_STEPS = 200_000

#: RK4's stability limit on the real axis: stable for `h*|lambda| <= 2.785`. Used by
#: `_steps_for_stability`, which keeps a fast reaction from being reported as a safe one.
RK4_REAL_STABILITY_LIMIT = 2.785

#: Headroom over the dosed reagent's quasi-steady concentration when deriving the step floor, for
#: RK4's transient and the co-reagent term `_stiffness_bound` leaves out. Enters the stiffness as
#: `margin^(n_d - 1)`.
QUASI_STEADY_MARGIN = 10.0

#: Default (minimum) step count. Sufficient to eight figures on a non-stiff dose;
#: `_steps_for_stability` raises it as the problem requires.
DEFAULT_INTEGRATION_STEPS = 200

#: The most points a `SemiBatchProfile` keeps: evenly spaced samples, the last instant and the
#: peak. The peak is tracked inside the loop, so memory is bounded by this, not by the step count.
PROFILE_POINTS = 25

#: The two schemes a `SemiBatchProfile` can come out of, returned as its `method`.
METHOD_RK4 = "rk4"
METHOD_STABLE = "sdirk3-l-stable"

#: The stable scheme's fixed step count. An L-stable scheme is stable at any step size, so the count
#: is set by the accuracy the slow part of the dose needs and the cost is constant however fast the
#: reaction. Agrees with fine-grid RK4 to better than 1e-7 on the reference fixtures.
STABLE_INTEGRATION_STEPS = 2_000

#: Backward-Euler sub-steps replacing the stable scheme's first step. The SDIRK stability function
#: is negative at large `h*lambda`, so starting from zero accumulation it would overshoot the
#: quasi-steady value and report that overshoot as the peak; backward Euler approaches from below.
STABLE_START_SUBSTEPS = 4

# Alexander's three-stage, third-order, L-stable, stiffly accurate SDIRK (SIAM J. Numer. Anal. 14,
# 1977, table 5.2). Stiffly accurate: the last stage is the step's answer, so neither species goes
# below zero.
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

#: Newton iterations per implicit stage; the bisection bracket closes regardless, so this bounds
#: cost rather than setting a tolerance.
_STAGE_ITERATIONS = 100

#: Conversions at or above this are refused: there the answer is set by mixing, impurities and the
#: reverse reaction, not the rate law.
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

    `value <= 0.0` is False for NaN, and pydantic's `gt=0` passes `Infinity`.
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

    The closed forms are derived for `n >= 0`.
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
        rate_constant: `k`, in the unit its order implies; not converted.
        initial_concentration: `C₀`, in the caller's own unit.
        time_seconds: How long the reaction runs.
        order: The order in the limiting reagent.

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

    # Ratio form `(C/C₀)^(1-n) = 1 + (n-1)·a` with `a = k·t·C₀^(n-1)` the Damköhler number, taken as
    # a logarithm: only the ratio (in [0, 1]) is exponentiated, so the absolute form's overflows at
    # extreme order or concentration cannot refuse an ordinary answer.
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
            # For n < 1 the concentration reaches zero in finite time — a real property of the rate
            # law, so reported as complete.
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

    The closed-form inverse of `batch_conversion`, so the two cannot drift apart.

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

    An ideal PFR is a batch reactor in residence time, so this calls `batch_conversion`.

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

    Solves the algebraic balance `C₀ - C = τ·k·Cⁿ` (the tank is at outlet composition): first order
    directly, second as a quadratic, every other order by bisection on a guaranteed bracket.

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

    # C₀ - C - τkCⁿ is strictly decreasing in C over (0, C₀], negative at C₀ and positive near 0, so
    # bisection always brackets the root. Only its sign is needed, compared in logarithms so
    # `τ·k·Cⁿ` cannot overflow when the outlet itself is an ordinary number.
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
    #: Unreacted dosed reagent present, as a fraction of the whole dose: what would react at once if
    #: cooling failed now.
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
    #: The highest unreacted fraction during the dose, and when: the design case for the dose time.
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

    `J_d = k*n_d*C_d^(n_d-1)*C_co^n_co`, evaluated at `_dosed_concentration_bound` and `C_co,0` (the
    co-reagent is only consumed and diluted). Orders below one return zero — their derivative is
    unbounded as the reagent runs out, and `semibatch_accumulation` refuses that case by name. The
    co-reagent's partial derivative is left out because it is small exactly when the reaction is
    fast; the divergence check catches the rare case where it matters.
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

    The quasi-steady concentration `C_d* = (F / (V * k * C_co^n_co))^(1/n_d)` at `V_0` and `C_co,0`:
    the dose starts at zero and cannot cross `C_d*(t)` from below, so this bounds `J_d` over the
    whole dose. Capped by the whole charge in the starting volume, for a slow reaction that never
    reaches quasi-steady state. At `n_d < 1` the result is unused and the charge is returned as-is.
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

    RK4 diverges once `h * lambda` passes `RK4_REAL_STABILITY_LIMIT`, and a divergence clamped at
    zero would report a fast reaction as having no accumulation at all. So the floor is derived from
    the problem's stiffness. Past `MAX_INTEGRATION_STEPS` this returns `None` and the dose goes to
    the stable scheme.

    Args:
        stiffness: `_stiffness_bound`'s upper bound on the Jacobian's eigenvalue, per second.
        dose_time_seconds: How long the addition takes.
        requested: The caller's step count, which is a floor rather than the answer.

    Returns:
        The RK4 step count, or `None` when no count up to `MAX_INTEGRATION_STEPS` is stable.

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

    Answers how much unreacted dosed reagent is present at the worst moment — what a cooling failure
    would have to absorb. Two states (moles of each reagent), volume rising linearly. RK4 at a
    problem-derived step count, or Alexander's L-stable SDIRK at `STABLE_INTEGRATION_STEPS` when RK4
    cannot be stable; the profile's `method` says which. Isothermal: the jacket is assumed to hold.

    Args:
        rate_constant: `k` for the reaction, in the unit the two orders imply.
        dose_time_seconds: How long the addition takes, at a constant rate.
        initial_volume: The volume in the vessel before dosing, in the caller's own unit.
        dosed_moles: Total moles of the dosed reagent added over the whole addition.
        dosed_volume: The volume that dose occupies, in the unit of `initial_volume`; 0 for neat.
        initial_coreagent_concentration: The co-reagent's concentration in the vessel at the start.
        order_in_dosed: Order in the dosed reagent.
        order_in_coreagent: Order in the co-reagent; 0 for a large excess.
        steps: Minimum integration steps.
        force_stable: Use the stable scheme even where RK4 would be stable (tests only).

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
    # Derived from the problem's stiffness; `steps` is the caller's floor, not the answer.
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

        Explicit because `0.0 ** 0.0` is 1. A float `**` overflow is caught here and refused by
        name, since no magnitude bound on the inputs is physical.
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
        # The feed is unconditional: the integration domain is exactly the dose. A
        # `time < dose_time_seconds` guard would switch the feed off for the last k4 stage alone and
        # drop RK4 to first-order convergence.
        return feed_rate - consumed, -consumed

    def implicit_stage(
        time: float, dosed_known: float, coreagent_known: float, weight: float, guess: float
    ) -> tuple[float, float]:
        """Solve one implicit stage: `Y = known + weight * f(time, Y)`, both species at once.

        `Y_d - Y_c` is fixed by the feed whatever the rate law, so the stage is the scalar equation
        `Y_d + weight*R = target` with `R >= 0` rising in `Y_d`: a unique, bracketed root. Newton
        inside the bracket, bisecting whenever it would leave, cannot diverge or go negative.
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

        The first step is `STABLE_START_SUBSTEPS` backward-Euler sub-steps. Each step ends by
        projecting a state truncation left below zero back onto the conserved line `dosed -
        coreagent = F*t - n_co,0` — unlike a clamp, this keeps the moles the feed fixes.
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

    # A negative mole count is a diverged integration, never a physical zero, so it is refused
    # rather than clamped. The tolerance is far above round-off. Kept as a backstop because an order
    # below one has an unbounded Jacobian no step floor covers; that case gets its own message
    # below.
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
