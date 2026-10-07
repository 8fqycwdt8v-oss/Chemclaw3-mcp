"""The readiness check: run every correlation against a relation it does not itself contain.

This server loads no corpus, so readiness checks each correlation against a relation written nowhere
in the module under test, which the implementation cannot satisfy by agreeing with itself:

- Gilliland at total reflux returns Fenske's `N_min` (holds Molokanov only).
- Fenske matches a stage-by-stage McCabe-Thiele descent at total reflux, which contains no
  logarithm.
- Underwood's bisected root reproduces the closed-form binary `R_min`.
- Gilliland is monotonic in reflux and bounded below by `N_min`; at `R_min` it refuses.
- Crystallisation matches the saturated-charge closed form `y = 1 - (S_cold/S_hot)·(1 - E/W)`.
- A first-order thermal approach closes 63.2% at `t = τ`, with half-life `τ·ln 2`.
- Zwietering's exponents are dimensionally homogeneous, and the function exhibits each as a log-log
  slope.
- P/V scale-up reduces to `N₂ = N₁(D₁/D₂)^(2/3)` under geometric similarity.
- Cake filtration is parabolic, and its reported rate is the derivative of its time.
- The two drying periods agree at the critical moisture.

No table is transcribed, so the `Dataset` names the modules and version rather than a corpus
checksum.
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

#: The version of the correlations this build serves; bump it by hand when one changes, so
#: `/healthz` tells pods apart.
CONSTANTS_VERSION = "1.0.0"

#: Relative gap allowed between Gilliland at 1e7 x `R_min` and Fenske; a sign or exponent error
#: moves it by a factor.
_TOTAL_REFLUX_TOLERANCE = 1.0e-6

#: Relative gap allowed between the bisected Underwood root and the binary closed form; a solver
#: that stopped early fails it.
_UNDERWOOD_TOLERANCE = 1.0e-9

#: Log-log step and tolerance for recovering exponents by second-order central differences; a
#: transposed exponent misses by 0.05 or more. Also used for the filtration rate against its own
#: time curve.
_EXPONENT_STEP = 1.0e-5
_EXPONENT_TOLERANCE = 1.0e-6

#: Stages the total-reflux descent takes; keeps the worked column's composition well inside `(0, 1)`
#: at both volatilities checked.
_FENSKE_DESCENT_STAGES = 8

#: The remaining checks are identities between closed forms, so only floating-point error is
#: allowed.
_IDENTITY_TOLERANCE = 1.0e-10

#: The worked column: a 95/5 split at alpha = 2.5 from an equimolar saturated-liquid feed (Fenske
#: 6.4269 stages, Underwood 1.1000, both checked by hand).
_COLUMN = {
    "relative_volatility": 2.5,
    "light_key_in_feed": 0.5,
    "light_key_in_distillate": 0.95,
    "light_key_in_bottoms": 0.05,
}

#: An ordinary slurry for the Zwietering slope probe; the check measures slopes, so the values do
#: not matter.
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

    A `RuntimeError`: the pod is wrong, not the caller; `connector_app` treats it as permanent and
    takes the pod out of service.
    """


def _relative(found: float, expected: float) -> float:
    """The relative gap between two numbers that should be the same number."""
    return abs(found - expected) / abs(expected)


def _worked_column_bounds() -> tuple[float, float]:
    """`N_min` and `R_min` for the worked column, computed rather than transcribed.

    Shared by several checks so the pair is never copied as literals that drift from `_COLUMN`.
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

    The total-reflux check cannot see a wrong `N_min` (Molokanov returns whatever it is given). This
    uses a McCabe-Thiele descent at total reflux: operating line `y_{n+1} = x_n` and equilibrium `y
    = alpha*x / (1 + (alpha - 1)*x)`, so `x_{n+1} = x_n / (alpha - (alpha - 1)*x_n)`. After `m`
    steps Fenske must report exactly `m` stages; a wrong logarithm base, sign or ratio breaks it.

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
            # Scale the density difference, the only way to move the buoyancy group g·(rho_s -
            # rho_L)/rho_L alone.
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

    No transcribed table exists, so the digest is over source: equal digests mean the same
    correlations, and a comment edit moves it.
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
        SelfTestFailed: A relation no longer holds; `connector_app` answers unready with the reason.
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
            # The correlations are the modules, so the module is what is named.
            records_path=Path(distillation.__file__),
        )
    ]
