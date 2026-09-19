"""The readiness check: run every correlation against a relation it does not itself contain.

This server loads no corpus, and
`D-2026-09-15-a-server-with-nothing-to-load-still-has-something-to-verify` already settled that
this does not excuse a probe. What it verifies is chosen on the principle
`servers/kinetics` and `servers/suitability` set: **prefer a check the implementation cannot
satisfy by agreeing with itself.**

Every check below is a relation written nowhere in the module it tests — the list is not counted
here, because `verify()` is what runs them and a number in prose is a claim about an afternoon:

- **A column at total reflux must reduce to Fenske.** As `R → ∞` Molokanov's form must give back
  the minimum stage count it was handed. That holds *Molokanov*, and it is all it holds: the check
  passes Fenske's own answer in and asserts it comes back, which the correlation does for whatever
  number it is given. Driven on this build, `ln alpha` transcribed as `log10 alpha` moved `N_min`
  from 6.426866 to 14.798406 — a factor of 2.303 — and the probe still passed.
- **So Fenske is checked separately, against a stage-by-stage descent at total reflux** that
  contains no logarithm: `y_{n+1} = x_n` for the operating line and `y = alpha·x/(1 + (alpha-1)·x)`
  for equilibrium. Stepping `m` stages down from the distillate lands on a composition Fenske must
  report as exactly `m` stages from it, and nothing inside `distillation.py` can satisfy that by
  agreeing with itself.
- **Underwood's root, found by bisection, must reproduce the closed-form binary minimum reflux.**
  `R_min = [x_D/z - alpha(1-x_D)/(1-z)]/(alpha-1)` for a saturated liquid feed is *not* in
  `distillation.py`: that module solves the θ-equation numerically and evaluates the second
  Underwood equation at the root. Two routes to one number, and they agree to machine precision or
  the solver has moved.
- **Gilliland must be monotonic in reflux**, and `N` must always exceed `N_min`. Both are
  properties of the correlation rather than of its transcription.
- **A minimum reflux is a floor.** At `R_min` the stage count is infinite, so the refusal at and
  below it is part of the arithmetic being right rather than a courtesy.
- **A crystallisation yield must match the closed form for a saturated charge**,
  `y = 1 - (S_cold/S_hot)·(1 - E/W)`, which the mass balance in `crystallisation.py` does not
  contain — and must go to zero as the two solubilities converge.
- **A first-order thermal approach must close 63.2% of its gap at `t = τ`**, and its half-life
  must be `τ·ln 2`. The time constant and the inversion that produces a time-to-target are
  separately written, so the identity ties them.
- **Zwietering's exponent set must be dimensionally homogeneous** — the length exponents summing to
  zero and the time exponents to -1, which is what makes `N_js` a frequency — and the *function*
  must exhibit each published exponent as a log-log sensitivity. That is a three-link chain:
  function against constants, constants against dimensional analysis. A transposed pair breaks the
  second link, and a misplaced exponent in the expression breaks the first.
- **A scale-up matching P/V must reduce to `N₂ = N₁(D₁/D₂)^(2/3)` under geometric similarity**, a
  rule `mixing.py` deliberately does not use because real vessels are not similar.
- **Cake filtration must be parabolic in volume** with no medium resistance — doubling the filtrate
  exactly quadruples the time — and the instantaneous rate it reports must be the derivative of the
  time it reports, checked by a central difference of one against the other.
- **The two drying periods must agree at the critical moisture.** The time to dry from `2X_c` to
  `X_c` at constant rate must equal the time to dry from `X_c` to `X_c/e` in the falling-rate
  period: both are `m_s·X_c/(A·N_c)`, by two separately written expressions, and the identity holds
  only if the falling rate is continuous with the constant rate at `X_c`.

There is nothing to digest here — this server transcribes no table. Every number it uses is a
physical constant, a definition or a published exponent, so the `Dataset` names the modules and the
version rather than a checksum over a corpus that does not exist.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from types import ModuleType

from mcp_server_kit.datasets import Dataset

from chemclaw_mcp_unitops.engine import (
    crystallisation,
    distillation,
    drying,
    filtration,
    heat,
    mixing,
    validation,
)

__all__ = ["CONSTANTS_VERSION", "SelfTestFailed", "verify"]

#: The version of the first-party correlations this build serves. Bumped by hand in the commit that
#: changes one, so an operator reading `/healthz` can tell two pods apart without a shell on either.
CONSTANTS_VERSION = "1.0.0"

#: How far Gilliland may sit from Fenske when the reflux is driven to ten million times the
#: minimum. Measured at that reflux on the worked column below: **9.5e-08** relative. The threshold
#: is 1e-06, an order of magnitude of daylight, because what it is guarding against is a sign or an
#: exponent error — which moves the answer by a factor, not by a part per million.
_TOTAL_REFLUX_TOLERANCE = 1.0e-6

#: How far the numeric Underwood root may sit from the closed-form binary minimum reflux. Measured
#: over the three columns below: **1.3e-15** relative at its worst, because the bisection closes on
#: the root long before its 200 halvings are spent. 1e-9 is what one that stopped early would still
#: have to beat.
_UNDERWOOD_TOLERANCE = 1.0e-9

#: The log-log step and tolerance for recovering Zwietering's exponents from the function. A
#: central difference in log space is second-order, so at a 1e-5 step the truncation error is of
#: order 1e-10; measured, the worst of the five recovers its exponent to **1.3e-11**. 1e-6 is the
#: threshold, which a transposed exponent misses by 0.05 or more. The same tolerance holds the
#: filtration rate against a finite difference of its own time curve, measured at **1.4e-10**.
_EXPONENT_STEP = 1.0e-5
_EXPONENT_TOLERANCE = 1.0e-6

#: How many stages the total-reflux descent below steps before it stops. Eight is what keeps the
#: worked column's composition inside `(0, 1)` with room to spare at both relative volatilities the
#: check uses: at alpha = 2.5 the eighth stage sits at x = 0.0050 and at alpha = 1.8 at x = 0.4745.
#: The check is an exact identity at every stage, so the count buys redundancy rather than reach.
_FENSKE_DESCENT_STAGES = 8

#: Everything else is an identity between two closed forms, so the only error is floating point.
#: Measured worst case across the five: **1.1e-16** relative. 1e-10 is the threshold.
_IDENTITY_TOLERANCE = 1.0e-10

#: The worked column every distillation check runs on: a 95/5 split of a pair at alpha = 2.5 from an
#: equimolar saturated-liquid feed. Fenske gives 6.4269 stages and Underwood 1.1000 — both
#: reproduced by hand from the textbook forms before this module was written.
_COLUMN = {
    "relative_volatility": 2.5,
    "light_key_in_feed": 0.5,
    "light_key_in_distillate": 0.95,
    "light_key_in_bottoms": 0.05,
}

#: A slurry for the Zwietering sensitivity probe. The values are ordinary — 200 µm crystals of a
#: 2500 kg/m³ solid in water at 10% loading on a 1/3 m impeller — and nothing in the check depends
#: on them being any particular number, because what it measures is a *slope*.
_SLURRY = {
    "impeller_diameter_m": 1.0 / 3.0,
    "particle_diameter_m": 2.0e-4,
    "particle_density_kg_per_m3": 2500.0,
    "liquid_density_kg_per_m3": 1000.0,
    "liquid_viscosity_pa_s": 1.0e-3,
    "solids_loading_percent": 10.0,
    "zwietering_constant": 7.5,
    "power_number": 5.0,
    "liquid_volume_m3": 0.785,
}


class SelfTestFailed(RuntimeError):
    """A relation this server's arithmetic no longer satisfies.

    `RuntimeError` rather than `ValueError`: this is never a caller's input, it is this pod being
    wrong, and `connector_app` classifies it as a permanent cause so the pod leaves its Service
    rather than serving correlations that have moved.
    """


def _relative(found: float, expected: float) -> float:
    """The relative gap between two numbers that should be the same number."""
    return abs(found - expected) / abs(expected)


def _worked_column_bounds() -> tuple[float, float]:
    """`N_min` and `R_min` for the worked column, computed rather than transcribed.

    Three checks below need this pair, and writing it out as two literals in each would be three
    copies of a number that moves whenever `_COLUMN` does — the defect this repository deletes port
    tables over, one probe down. Measured at **9.6 µs** to derive, against a 302 µs whole probe.
    """
    minimum_stages = distillation.fenske_minimum_stages(
        relative_volatility=_COLUMN["relative_volatility"],
        light_key_in_distillate=_COLUMN["light_key_in_distillate"],
        light_key_in_bottoms=_COLUMN["light_key_in_bottoms"],
    )
    minimum_reflux, _ = distillation.underwood_minimum_reflux(
        relative_volatility=_COLUMN["relative_volatility"],
        light_key_in_feed=_COLUMN["light_key_in_feed"],
        light_key_in_distillate=_COLUMN["light_key_in_distillate"],
    )
    return minimum_stages, minimum_reflux


def _check_total_reflux_reduces_to_fenske() -> None:
    """Gilliland at effectively infinite reflux must give back Fenske's minimum stage count."""
    minimum_stages, minimum_reflux = _worked_column_bounds()
    at_total = distillation.gilliland_stages(
        minimum_stages=minimum_stages,
        minimum_reflux=minimum_reflux,
        reflux_ratio=1.0e7 * minimum_reflux,
    )
    gap = _relative(at_total, minimum_stages)
    if gap > _TOTAL_REFLUX_TOLERANCE:
        raise SelfTestFailed(
            f"at ten million times the minimum reflux the Gilliland correlation gives "
            f"{at_total:.6f} stages where Fenske's total-reflux algebra gives "
            f"{minimum_stages:.6f} — a relative gap of {gap:.2e}. A column at total reflux is a "
            "Fenske column by definition, so one of the two has moved."
        )


def _check_fenske_against_a_stage_by_stage_descent() -> None:
    """Fenske's `N_min` against a descent it shares no line of code with.

    **The total-reflux check above cannot see a wrong `N_min`, and read as though it could.** It
    takes Fenske's own answer, hands it to `gilliland_stages` at ten million times the minimum
    reflux, and asserts the correlation gives it back — which Molokanov's form does for *whatever*
    number it is given. Driven on this build with `ln alpha` transcribed as `log10 alpha`, `N_min`
    on the worked column moved from 6.426866 to 14.798406 stages, a factor of 2.303, and `verify()`
    still returned its dataset: `/healthz` answered 200 on a pod whose minimum stage count was
    wrong by a factor. The other three servers' probes catch an equivalent corruption and answer
    503 naming it.

    What this check contains instead is a McCabe-Thiele descent at total reflux, which is two
    relations and no logarithm: the operating line is `y_{n+1} = x_n`, and equilibrium at constant
    relative volatility is `y = alpha*x / (1 + (alpha - 1)*x)`. Eliminating `y` gives
    `x_{n+1} = x_n / (alpha - (alpha - 1)*x_n)`, so stepping `m` stages down from the distillate
    composition lands on a composition that Fenske must report as exactly `m` stages away from it.
    A wrong logarithm base, a wrong sign or an inverted ratio breaks that; nothing internal to
    `distillation.py` can satisfy it, because the descent is written here and the logarithm is not.

    Raises:
        SelfTestFailed: If Fenske disagrees with the descent at any stage.
    """
    for alpha, distillate in (
        (_COLUMN["relative_volatility"], _COLUMN["light_key_in_distillate"]),
        (1.8, 0.99),
    ):
        composition = distillate
        for stages_down in range(1, _FENSKE_DESCENT_STAGES + 1):
            composition = composition / (alpha - (alpha - 1.0) * composition)
            reported = distillation.fenske_minimum_stages(
                relative_volatility=alpha,
                light_key_in_distillate=distillate,
                light_key_in_bottoms=composition,
            )
            if _relative(reported, float(stages_down)) > _IDENTITY_TOLERANCE:
                raise SelfTestFailed(
                    f"stepping {stages_down} stage(s) down from x = {distillate} at total reflux "
                    f"with alpha = {alpha} reaches x = {composition:.12f}, and Fenske reports that "
                    f"pair as {reported:.12f} minimum stages rather than {stages_down}. The "
                    "descent is the equilibrium curve and the y = x operating line and holds no "
                    "logarithm, so it is the minimum-stage algebra that has moved."
                )


def _check_underwood_against_its_closed_form() -> None:
    """The bisected θ-root must reproduce a binary closed form this server does not hold."""
    for alpha, feed, top in ((2.5, 0.5, 0.95), (1.8, 0.35, 0.99), (4.0, 0.6, 0.90)):
        found, theta = distillation.underwood_minimum_reflux(
            relative_volatility=alpha, light_key_in_feed=feed, light_key_in_distillate=top
        )
        closed = (top / feed - alpha * (1.0 - top) / (1.0 - feed)) / (alpha - 1.0)
        if _relative(found, closed) > _UNDERWOOD_TOLERANCE:
            raise SelfTestFailed(
                f"at alpha = {alpha} the θ-equation was solved at θ = {theta:.9f} "
                f"and gives a minimum reflux of {found:.9f}, where the closed form"
                f" for a binary saturated-liquid feed gives {closed:.9f}. The root"
                " or the second Underwood equation has moved."
            )


def _check_gilliland_is_monotonic_and_bounded_below_by_fenske() -> None:
    """More reflux must always buy stages, and no reflux buys more than total reflux."""
    minimum_stages, minimum_reflux = _worked_column_bounds()
    previous = math.inf
    for multiple in (1.01, 1.05, 1.1, 1.3, 1.5, 2.0, 5.0, 20.0):
        stages = distillation.gilliland_stages(
            minimum_stages=minimum_stages,
            minimum_reflux=minimum_reflux,
            reflux_ratio=multiple * minimum_reflux,
        )
        if stages >= previous:
            raise SelfTestFailed(
                f"raising the reflux to {multiple}x the minimum needs {stages:.4f} stages against "
                f"{previous:.4f} at the step before. The Gilliland correlation is monotonic in "
                "reflux, so this is not a tolerance question."
            )
        if stages <= minimum_stages:
            raise SelfTestFailed(
                f"at {multiple}x the minimum reflux the correlation gives {stages:.4f} stages, "
                f"below the total-reflux minimum of {minimum_stages}. No finite reflux beats total "
                "reflux."
            )
        previous = stages


def _check_the_minimum_reflux_is_a_floor() -> None:
    """At the minimum the stage count is infinite, so a number returned there would be wrong."""
    minimum_stages, minimum_reflux = _worked_column_bounds()
    try:
        distillation.gilliland_stages(
            minimum_stages=minimum_stages,
            minimum_reflux=minimum_reflux,
            reflux_ratio=minimum_reflux,
        )
    except validation.UnitOpsInputError:
        return
    raise SelfTestFailed(
        "the correlation returned a finite stage count at exactly the minimum reflux, where the "
        "stage count is infinite. A column run at R_min never reaches its specification."
    )


def _check_crystallisation_against_the_saturated_closed_form() -> None:
    """A saturated charge has a closed-form yield the mass balance does not contain."""
    solvent, hot = 10.0, 0.30
    for cold, evaporated in ((0.05, 0.0), (0.05, 2.0), (0.02, 3.5), (hot * (1.0 - 1.0e-9), 0.0)):
        found = crystallisation.crystallisation_yield(
            solute_charged_kg=hot * solvent,
            solvent_charged_kg=solvent,
            solubility_hot_kg_per_kg_solvent=hot,
            solubility_cold_kg_per_kg_solvent=cold,
            solvent_evaporated_kg=evaporated,
        ).yield_fraction
        closed = 1.0 - (cold / hot) * (1.0 - evaporated / solvent)
        if abs(found - closed) > _IDENTITY_TOLERANCE:
            raise SelfTestFailed(
                f"a saturated charge cooled from a solubility of {hot} to {cold} kg/kg with "
                f"{evaporated} kg of solvent removed yields {found:.12f} by the mass balance and "
                f"{closed:.12f} by the closed form. The balance has moved."
            )


def _check_the_first_order_approach() -> None:
    """63.2% of the gap at one time constant, and a half-life of τ·ln 2."""
    vessel = {
        "batch_mass_kg": 200.0,
        "heat_capacity_j_per_kg_k": 1900.0,
        "overall_heat_transfer_coefficient_w_per_m2_k": 300.0,
        "heat_transfer_area_m2": 2.1,
        "initial_temperature_c": 60.0,
        "jacket_temperature_c": 5.0,
    }
    gap = 55.0
    tau = heat.time_constant(**vessel).time_constant_seconds
    at_tau = heat.time_constant(**vessel, time_seconds=tau).temperature_after_c
    if at_tau is None:  # pragma: no cover - the argument was supplied, so this cannot be None
        raise SelfTestFailed("the transient returned no temperature for a time it was given")
    closed = (60.0 - at_tau) / gap
    if abs(closed - heat.APPROACH_AT_ONE_TIME_CONSTANT) > _IDENTITY_TOLERANCE:
        raise SelfTestFailed(
            f"at one time constant the batch has closed {closed:.12f} of its gap to the jacket, "
            f"where every first-order system closes {heat.APPROACH_AT_ONE_TIME_CONSTANT:.12f}."
        )
    half = heat.time_constant(**vessel, target_temperature_c=5.0 + gap / 2.0).time_to_target_seconds
    if half is None:  # pragma: no cover - the argument was supplied, so this cannot be None
        raise SelfTestFailed("the transient returned no time for a target it was given")
    if _relative(half, tau * math.log(2.0)) > _IDENTITY_TOLERANCE:
        raise SelfTestFailed(
            f"halving the gap to the jacket takes {half:.6f} s against a time constant of "
            f"{tau:.6f} s, where the half-life of a first-order approach is τ·ln2 = "
            f"{tau * math.log(2.0):.6f} s. The time constant and its inversion disagree."
        )


def _check_scale_up_reduces_to_the_similarity_rule() -> None:
    """Between geometrically similar vessels, matching P/V is `N₂ = N₁(D₁/D₂)^(2/3)`."""
    small_diameter, large_diameter, small_volume, speed = 0.10, 0.60, 1.0e-3, 500.0
    scaled = mixing.agitation_scale_up(
        small_impeller_diameter_m=small_diameter,
        small_speed_rpm=speed,
        small_liquid_volume_m3=small_volume,
        large_impeller_diameter_m=large_diameter,
        large_liquid_volume_m3=small_volume * (large_diameter / small_diameter) ** 3,
        power_number=5.0,
        liquid_density_kg_per_m3=1000.0,
        liquid_viscosity_pa_s=1.0e-3,
    )
    expected = speed * (small_diameter / large_diameter) ** (2.0 / 3.0)
    found = scaled.matched_power_per_volume.speed_rpm
    if _relative(found, expected) > _IDENTITY_TOLERANCE:
        raise SelfTestFailed(
            f"matching P/V between geometrically similar vessels gives {found:.9f} rpm where the "
            f"similarity rule N₂ = N₁(D₁/D₂)^(2/3) gives {expected:.9f} rpm."
        )
    tip = scaled.matched_tip_speed
    if _relative(tip.tip_speed_m_per_s, scaled.small.tip_speed_m_per_s) > _IDENTITY_TOLERANCE:
        raise SelfTestFailed(
            f"the tip-speed-matched large vessel runs at {tip.tip_speed_m_per_s:.9f} m/s against "
            f"{scaled.small.tip_speed_m_per_s:.9f} m/s at the small scale. Matching tip speed is "
            "exact, so these are the same number or the criterion is not being applied."
        )


def _zwietering_slope(argument: str, factor: float) -> float:
    """The log-log sensitivity of `N_js` to one input, by a central difference in log space."""

    def speed(scale: float) -> float:
        inputs = dict(_SLURRY)
        if argument == "buoyancy":
            # Scaling the *density difference* rather than either density: the correlation's
            # buoyancy group is g·(rho_s - rho_L)/rho_L, so this is the only way to move
            # that group alone.
            difference = _SLURRY["particle_density_kg_per_m3"] - _SLURRY["liquid_density_kg_per_m3"]
            inputs["particle_density_kg_per_m3"] = (
                _SLURRY["liquid_density_kg_per_m3"] + scale * difference
            )
        else:
            inputs[argument] = _SLURRY[argument] * scale
        return mixing.just_suspended_speed(**inputs).speed_rev_per_s

    up, down = speed(1.0 + factor), speed(1.0 - factor)
    return math.log(up / down) / math.log((1.0 + factor) / (1.0 - factor))


def _check_zwietering_is_dimensionally_homogeneous_and_exhibits_its_exponents() -> None:
    """The exponent set must make a frequency, and the function must show each exponent."""
    exponents = mixing.ZWIETERING_EXPONENTS
    # The kinematic viscosity nu is m²/s, d_p is m, the buoyancy group g·(rho_s - rho_L)/rho_L
    # is m/s², X is dimensionless and D is m.
    length = (
        2.0 * exponents["kinematic_viscosity"]
        + exponents["particle_diameter"]
        + exponents["buoyancy"]
        + exponents["impeller_diameter"]
    )
    seconds = -exponents["kinematic_viscosity"] - 2.0 * exponents["buoyancy"]
    if abs(length) > _IDENTITY_TOLERANCE or abs(seconds + 1.0) > _IDENTITY_TOLERANCE:
        raise SelfTestFailed(
            f"Zwietering's exponents no longer make a frequency: the length exponents sum to "
            f"{length:+.4f} where they must cancel, and the time exponents to {seconds:+.4f} where "
            "they must give -1. A transposed pair reads as a plausible speed and is not one."
        )
    measured = {
        "kinematic_viscosity": _zwietering_slope("liquid_viscosity_pa_s", _EXPONENT_STEP),
        "particle_diameter": _zwietering_slope("particle_diameter_m", _EXPONENT_STEP),
        "buoyancy": _zwietering_slope("buoyancy", _EXPONENT_STEP),
        "solids_loading": _zwietering_slope("solids_loading_percent", _EXPONENT_STEP),
        "impeller_diameter": _zwietering_slope("impeller_diameter_m", _EXPONENT_STEP),
    }
    for name, published in exponents.items():
        if abs(measured[name] - published) > _EXPONENT_TOLERANCE:
            raise SelfTestFailed(
                f"N_js responds to {name} as a power of {measured[name]:.6f} where Zwietering's "
                f"published exponent is {published}. The expression and the constants disagree."
            )


def _check_filtration_is_parabolic_and_reports_its_own_derivative() -> None:
    """Doubling the filtrate quadruples a cake-limited time, and the rate is dt/dV inverted."""
    cake = {
        "filter_area_m2": 0.456,
        "pressure_drop_pa": 8.0e4,
        "filtrate_viscosity_pa_s": 1.2e-3,
        "specific_cake_resistance_m_per_kg": 5.0e11,
        "dry_cake_per_filtrate_kg_per_m3": 120.0,
    }
    single = filtration.filtration_time(filtrate_volume_m3=0.1, **cake).total_time_seconds
    double = filtration.filtration_time(filtrate_volume_m3=0.2, **cake).total_time_seconds
    if _relative(double / single, 4.0) > _IDENTITY_TOLERANCE:
        raise SelfTestFailed(
            f"doubling the filtrate volume through a cake with no medium resistance multiplied the "
            f"time by {double / single:.9f} rather than by 4. Cake filtration at constant pressure "
            "is parabolic in volume; a linear answer means the cake term has lost its square."
        )

    with_medium = {**cake, "medium_resistance_per_m": 2.0e10}
    volume, step = 0.25, 1.0e-7
    reported = filtration.filtration_time(
        filtrate_volume_m3=volume, **with_medium
    ).final_rate_m3_per_s
    ahead = filtration.filtration_time(
        filtrate_volume_m3=volume + step, **with_medium
    ).total_time_seconds
    behind = filtration.filtration_time(
        filtrate_volume_m3=volume - step, **with_medium
    ).total_time_seconds
    differenced = 2.0 * step / (ahead - behind)
    if _relative(reported, differenced) > _EXPONENT_TOLERANCE:
        raise SelfTestFailed(
            f"the filtration reports a final rate of {reported:.9e} m³/s, where differentiating "
            f"its own time-against-volume curve gives {differenced:.9e} m³/s. The integrated law "
            "and the differential law have come apart."
        )


def _check_the_drying_periods_agree_at_the_critical_moisture() -> None:
    """Both periods are `m_s·X_c/(A·N_c)` over their own characteristic span, by two expressions."""
    dryer = {
        "dry_solid_mass_kg": 80.0,
        "drying_area_m2": 1.2,
        "constant_rate_kg_per_m2_s": 5.0e-4,
    }
    critical = 0.10
    constant_leg = drying.drying_time(
        **dryer,
        initial_moisture_dry_basis=2.0 * critical,
        critical_moisture_dry_basis=critical,
        final_moisture_dry_basis=critical,
    ).constant_rate_seconds
    falling_leg = drying.drying_time(
        **dryer,
        initial_moisture_dry_basis=critical,
        critical_moisture_dry_basis=critical,
        final_moisture_dry_basis=critical / math.e,
    ).falling_rate_seconds
    if _relative(falling_leg, constant_leg) > _IDENTITY_TOLERANCE:
        raise SelfTestFailed(
            f"drying from 2·X_c to X_c at constant rate takes {constant_leg:.6f} s while drying "
            f"from X_c to X_c/e in the falling-rate period takes {falling_leg:.6f} s. Both are "
            "m_s·X_c/(A·N_c), so the falling rate is no longer continuous with the constant rate "
            "at the critical moisture."
        )


def _formula_digest() -> str:
    """A digest over this server's engine modules.

    There is no transcribed table to digest field by field — every number here is a physical
    constant, a definition or a published exponent. So the digest is over the source, and it says
    what it is: two pods agreeing means they run the same correlations, and a comment change moves
    it. That is weaker than a value digest and is the honest thing available, which is the same
    answer `servers/kinetics` reached for the same reason.
    """
    digest = hashlib.sha256()
    modules: tuple[ModuleType, ...] = (
        crystallisation,
        distillation,
        drying,
        filtration,
        heat,
        mixing,
        validation,
    )
    for module in modules:
        source = module.__file__
        if source is None:  # pragma: no cover - only for a module with no file, e.g. frozen
            raise SelfTestFailed(
                f"{module.__name__} has no source file, so this pod cannot say which correlations "
                "it is serving"
            )
        digest.update(Path(source).read_bytes())
    return digest.hexdigest()


def verify() -> list[Dataset]:
    """Recompute what this server is made of, against relations it does not contain.

    Returns:
        One `Dataset` naming the correlations this pod serves.

    Raises:
        SelfTestFailed: If any relation no longer holds. `connector_app` turns that into an unready
            `/healthz` naming the reason.
    """
    _check_total_reflux_reduces_to_fenske()
    _check_fenske_against_a_stage_by_stage_descent()
    _check_underwood_against_its_closed_form()
    _check_gilliland_is_monotonic_and_bounded_below_by_fenske()
    _check_the_minimum_reflux_is_a_floor()
    _check_crystallisation_against_the_saturated_closed_form()
    _check_the_first_order_approach()
    _check_scale_up_reduces_to_the_similarity_rule()
    _check_zwietering_is_dimensionally_homogeneous_and_exhibits_its_exponents()
    _check_filtration_is_parabolic_and_reports_its_own_derivative()
    _check_the_drying_periods_agree_at_the_critical_moisture()

    return [
        Dataset(
            name="unitops-correlations",
            version=CONSTANTS_VERSION,
            licence="first-party",
            retrieved_from=(
                "Fenske (1932), Underwood (1948) and Gilliland (1940) in Molokanov's closed form, "
                "as given in Seader & Henley, Separation Process Principles; Zwietering (1958), "
                "Chem. Eng. Sci. 8, 244; the constant-pressure cake filtration and two-period "
                "batch drying models as given in Coulson & Richardson's Chemical Engineering "
                "Volume 2; the lumped first-order jacketed-vessel energy balance"
            ),
            description=(
                "The unit-operation correlations this server computes with. Verified on every "
                "probe against relations the implementation does not contain: a column at total "
                "reflux reducing to Fenske, Fenske's minimum stages against a stage-by-stage "
                "descent at total reflux, Underwood's numeric root against the closed-form "
                "binary minimum reflux, a crystallisation yield against the saturated-charge "
                "closed form, the 63.2% first-order approach, the geometric-similarity scale-up "
                "rule, Zwietering's dimensional homogeneity, a parabolic filtration reporting its "
                "own derivative, and the two drying periods meeting at the critical moisture."
            ),
            sha256=_formula_digest(),
            # The correlations *are* the modules, so a module is what is named. Pointing this at a
            # records file that does not exist would be provenance that quietly says nothing.
            records_path=Path(distillation.__file__),
        )
    ]
