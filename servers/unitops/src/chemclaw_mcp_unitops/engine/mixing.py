"""Agitation scale-up: power per volume, tip speed, and Zwietering's just-suspended speed.

**Power and tip speed are definitions.** In turbulent flow the ungassed power is `P = N_p·rho·N³·D⁵`
(`N` in rev/s) and tip speed `π·N·D`. `N_p` is the impeller's published figure and is never guessed.
`N_p` is constant only above Re ≈ 10⁴, so every answer carries the impeller Reynolds number
`rho·N·D²/µ` and says which side it is on.

**Zwietering's `N_js` is a fitted correlation** (1958, sand and salt in flat-bottomed vessels):

    N_js = S·nu^0.1·d_p^0.2·(g·(rho_s - rho_L)/rho_L)^0.45·X^0.13/D^0.85

`S` is a geometry constant read from the impeller's table and is a required input. The criterion is
visual (no particle resting on the base for more than 1-2 s): suspension, not homogeneity.

**Scale-up criteria disagree.** Equal `P/V` and equal tip speed cannot both hold at a new diameter,
and which matters depends on what limits; both are computed and the disagreement reported, never one
chosen silently.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from chemclaw_mcp_unitops.engine.validation import GRAVITY_M_PER_S2, finite_result, positive

__all__ = [
    "TURBULENT_REYNOLDS",
    "ZWIETERING_EXPONENTS",
    "AgitationDuty",
    "AgitationScaleUp",
    "JustSuspended",
    "agitation_scale_up",
    "just_suspended_speed",
]

#: Above this impeller Reynolds number the power number is effectively constant. Below it the answer
#: is flagged rather than refused, since viscous slurries are real questions.
TURBULENT_REYNOLDS = 1.0e4

#: Zwietering's exponents: kinematic viscosity, particle diameter, buoyancy group, solids loading,
#: impeller diameter. A mapping so `engine/selftest.py` can check dimensional homogeneity (length
#: exponents sum to zero, time to -1), which a transposed pair breaks.
ZWIETERING_EXPONENTS = {
    "kinematic_viscosity": 0.1,
    "particle_diameter": 0.2,
    "buoyancy": 0.45,
    "solids_loading": 0.13,
    "impeller_diameter": -0.85,
}

#: The bands Zwietering's experiments covered, `(low, high)` in this module's units, as commonly
#: quoted from the literature: 0.2-20% solids on a liquid mass basis, 125-850 µm particles. An input
#: outside is reported, not refused (the correlation is routinely stretched). The check matters
#: because `X^0.13` is weak: a 10% loading entered as `0.10` gives an `N_js` about 45% low that
#: still looks plausible, an under-agitated vessel.
ZWIETERING_FITTED_RANGES: dict[str, tuple[float, float]] = {
    "solids_loading_percent": (0.2, 20.0),
    "particle_diameter_m": (1.25e-4, 8.5e-4),
}


@dataclass(frozen=True)
class AgitationDuty:
    """One vessel's agitation duty at one speed."""

    speed_rpm: float
    tip_speed_m_per_s: float
    power_w: float
    power_per_volume_w_per_m3: float
    reynolds_number: float
    turbulent: bool


@dataclass(frozen=True)
class AgitationScaleUp:
    """The small vessel's duty, and the large vessel's under each matching criterion."""

    small: AgitationDuty
    #: The large vessel run at the speed that reproduces the small vessel's P/V.
    matched_power_per_volume: AgitationDuty
    #: The large vessel run at the speed that reproduces the small vessel's tip speed.
    matched_tip_speed: AgitationDuty
    #: The ratio of the two large-scale speeds; 1.0 only if the diameters are equal.
    criteria_disagree_by: float


def _duty(
    *,
    speed_rev_per_s: float,
    diameter_m: float,
    volume_m3: float,
    power_number: float,
    density_kg_per_m3: float,
    viscosity_pa_s: float,
) -> AgitationDuty:
    """Power, tip speed and Reynolds number for one impeller at one speed."""
    power = finite_result(
        lambda: power_number * density_kg_per_m3 * speed_rev_per_s**3 * diameter_m**5,
        "the impeller power",
    )
    reynolds = finite_result(
        lambda: density_kg_per_m3 * speed_rev_per_s * diameter_m**2 / viscosity_pa_s,
        "the Reynolds number",
    )
    return AgitationDuty(
        speed_rpm=speed_rev_per_s * 60.0,
        tip_speed_m_per_s=math.pi * speed_rev_per_s * diameter_m,
        power_w=power,
        power_per_volume_w_per_m3=power / volume_m3,
        reynolds_number=reynolds,
        turbulent=reynolds >= TURBULENT_REYNOLDS,
    )


def agitation_scale_up(
    *,
    small_impeller_diameter_m: float,
    small_speed_rpm: float,
    small_liquid_volume_m3: float,
    large_impeller_diameter_m: float,
    large_liquid_volume_m3: float,
    power_number: float,
    liquid_density_kg_per_m3: float,
    liquid_viscosity_pa_s: float,
) -> AgitationScaleUp:
    """Carry an agitation duty to another vessel, under both of the usual matching criteria.

    Matching `P/V` uses the two **supplied** volumes rather than assumed geometric similarity:

        N₂ = [ (P/V)₁ · V₂ / (N_p · rho · D₂⁵) ]^(1/3)

    which reduces to `N₂ = N₁·(D₁/D₂)^(2/3)` when `V ∝ D³` (checked by `engine/selftest.py`); a
    round-bottomed flask and a plant vessel are not similar. Matching tip speed is `N₂ = N₁·D₁/D₂`.

    Args:
        small_impeller_diameter_m: Impeller (not vessel) diameter at the small scale, m.
        small_speed_rpm: Agitator speed at the small scale, rpm.
        small_liquid_volume_m3: Liquid volume at the small scale, m³ (1 L = 0.001 m³).
        large_impeller_diameter_m: Impeller diameter at the large scale, m.
        large_liquid_volume_m3: Liquid volume at the large scale, m³.
        power_number: The impeller's turbulent power number `N_p`, from its own data. No default.
        liquid_density_kg_per_m3: Liquid density, kg/m³.
        liquid_viscosity_pa_s: Liquid dynamic viscosity, Pa·s (1 cP = 0.001 Pa·s), for the Reynolds
        number.

    Returns:
        The small vessel's duty and the large vessel's under each criterion, with the ratio between
        the two large-scale speeds.

    Raises:
        UnitOpsInputError: Any dimension, speed, volume, density, viscosity or power number is not
        positive.
    """
    for value, name in (
        (small_impeller_diameter_m, "the small-scale impeller diameter"),
        (small_speed_rpm, "the small-scale speed"),
        (small_liquid_volume_m3, "the small-scale liquid volume"),
        (large_impeller_diameter_m, "the large-scale impeller diameter"),
        (large_liquid_volume_m3, "the large-scale liquid volume"),
        (power_number, "the power number"),
        (liquid_density_kg_per_m3, "the liquid density"),
        (liquid_viscosity_pa_s, "the liquid viscosity"),
    ):
        positive(value, name)

    common = {
        "power_number": power_number,
        "density_kg_per_m3": liquid_density_kg_per_m3,
        "viscosity_pa_s": liquid_viscosity_pa_s,
    }
    small_speed = small_speed_rpm / 60.0
    small = _duty(
        speed_rev_per_s=small_speed,
        diameter_m=small_impeller_diameter_m,
        volume_m3=small_liquid_volume_m3,
        **common,
    )

    wanted_power = small.power_per_volume_w_per_m3 * large_liquid_volume_m3
    speed_for_power = finite_result(
        lambda: (
            (
                wanted_power
                / (power_number * liquid_density_kg_per_m3 * large_impeller_diameter_m**5)
            )
            ** (1.0 / 3.0)
        ),
        "the large-scale speed that matches P/V",
    )
    speed_for_tip = small_speed * small_impeller_diameter_m / large_impeller_diameter_m

    large_common = {
        "diameter_m": large_impeller_diameter_m,
        "volume_m3": large_liquid_volume_m3,
        **common,
    }
    return AgitationScaleUp(
        small=small,
        matched_power_per_volume=_duty(speed_rev_per_s=speed_for_power, **large_common),
        matched_tip_speed=_duty(speed_rev_per_s=speed_for_tip, **large_common),
        criteria_disagree_by=speed_for_power / speed_for_tip,
    )


@dataclass(frozen=True)
class JustSuspended:
    """Zwietering's just-suspended speed, and what it costs to run there."""

    speed_rpm: float
    speed_rev_per_s: float
    tip_speed_m_per_s: float
    #: Ungassed power at `N_js`, `P = N_p·rho·N³·D⁵`; the implied P/V is what scale-up turns on.
    power_w: float
    power_per_volume_w_per_m3: float
    reynolds_number: float
    turbulent: bool
    #: Whether every banded input sits inside its fitted band: prediction versus extrapolation,
    #: invisible in `N_js` itself.
    within_fitted_range: bool
    #: One sentence per out-of-band input; empty exactly when `within_fitted_range` is true.
    outside_fitted_range: tuple[str, ...]


def _outside_the_fitted_bands(
    *, solids_loading_percent: float, particle_diameter_m: float
) -> tuple[str, ...]:
    """Which inputs sit outside the band Zwietering regressed them over, named one per sentence.

    Separate from the correlation so a test can drive the boundaries alone.

    Args:
        solids_loading_percent: `X`, 100 x (mass solids / mass liquid).
        particle_diameter_m: `d_p`, in metres.

    Returns:
        One sentence per out-of-band input, in `ZWIETERING_FITTED_RANGES` order; empty when all are
        inside.
    """
    values = {
        "solids_loading_percent": (
            solids_loading_percent,
            "X, percent solids on a liquid basis",
            "A loading given as a mass fraction where this percentage is asked for lands below the "
            "band and returns a speed that is too low: measured, 10 wt% entered as 0.10 gives an "
            "N_js 45.05% low and a P/V 83.40% low, which is an under-agitated vessel.",
        ),
        "particle_diameter_m": (
            particle_diameter_m,
            "d_p, the particle diameter in metres",
            "A size given in micrometres or millimetres where metres are asked for lands outside "
            "the band by three orders of magnitude.",
        ),
    }
    notes: list[str] = []
    for name, (low, high) in ZWIETERING_FITTED_RANGES.items():
        value, described, hint = values[name]
        if not low <= value <= high:
            side = "below" if value < low else "above"
            notes.append(
                f"{described} is {value:g}, {side} the {low:g} to {high:g} band Zwietering's own "
                f"experiments covered, so N_js here is an extrapolation rather than a prediction. "
                f"{hint}"
            )
    return tuple(notes)


def just_suspended_speed(
    *,
    impeller_diameter_m: float,
    particle_diameter_m: float,
    particle_density_kg_per_m3: float,
    liquid_density_kg_per_m3: float,
    liquid_viscosity_pa_s: float,
    solids_loading_percent: float,
    zwietering_constant: float,
    power_number: float,
    liquid_volume_m3: float,
) -> JustSuspended:
    """Zwietering's `N_js`: the speed at which no particle rests on the vessel base.

    A fitted correlation over sand and salt in flat-bottomed baffled vessels, with a visual
    criterion. The usually quoted accuracy is about ±10% on its own data (a literature figure); a
    dished base, cohesive or needle-shaped solids, or out-of-range inputs can be well outside it.
    Out-of-band inputs are flagged in `within_fitted_range` / `outside_fitted_range`, not refused.

    Args:
        impeller_diameter_m: Impeller diameter `D`, in metres.
        particle_diameter_m: Particle diameter `d_p`, m (200 µm is 2.0e-4); for a distribution,
        usually the mass-median. particle_density_kg_per_m3: The *crystal* density, kg/m³, not bulk
        or tapped. liquid_density_kg_per_m3: Liquid density, kg/m³. liquid_viscosity_pa_s: Liquid
        dynamic viscosity, Pa·s (1 cP = 0.001 Pa·s), converted to kinematic. solids_loading_percent:
        `X`, 100 x (mass solids / mass liquid). **A percentage, on a liquid basis**: 10 kg in 100 kg
        solvent is 10. zwietering_constant: `S` for this impeller type, `T/D` and clearance, from
        Zwietering's or the vendor's table (roughly 2-15). No default. power_number: The impeller's
        turbulent power number, for the power at `N_js`. liquid_volume_m3: Liquid volume, in m³, for
        the P/V at `N_js`.

    Returns:
        The just-suspended speed and what running there costs in power and tip speed.

    Raises:
        UnitOpsInputError: Any input is not positive, including a zero density difference (a
        neutrally buoyant solid does not settle, and zero would read as "no agitation needed").
    """
    for value, name in (
        (impeller_diameter_m, "the impeller diameter"),
        (particle_diameter_m, "the particle diameter"),
        (particle_density_kg_per_m3, "the particle density"),
        (liquid_density_kg_per_m3, "the liquid density"),
        (liquid_viscosity_pa_s, "the liquid viscosity"),
        (solids_loading_percent, "the solids loading"),
        (zwietering_constant, "the Zwietering geometry constant S"),
        (power_number, "the power number"),
        (liquid_volume_m3, "the liquid volume"),
    ):
        positive(value, name)

    density_difference = particle_density_kg_per_m3 - liquid_density_kg_per_m3
    positive(
        density_difference,
        "the density difference (particle density minus liquid density), which must be positive "
        "because a solid that does not settle has no just-suspended speed and this correlation "
        "does not describe one that floats;",
    )

    kinematic_viscosity = liquid_viscosity_pa_s / liquid_density_kg_per_m3
    buoyancy = GRAVITY_M_PER_S2 * density_difference / liquid_density_kg_per_m3
    speed = finite_result(
        lambda: (
            zwietering_constant
            * kinematic_viscosity ** ZWIETERING_EXPONENTS["kinematic_viscosity"]
            * particle_diameter_m ** ZWIETERING_EXPONENTS["particle_diameter"]
            * buoyancy ** ZWIETERING_EXPONENTS["buoyancy"]
            * solids_loading_percent ** ZWIETERING_EXPONENTS["solids_loading"]
            * impeller_diameter_m ** ZWIETERING_EXPONENTS["impeller_diameter"]
        ),
        "the just-suspended speed",
    )
    duty = _duty(
        speed_rev_per_s=speed,
        diameter_m=impeller_diameter_m,
        volume_m3=liquid_volume_m3,
        power_number=power_number,
        density_kg_per_m3=liquid_density_kg_per_m3,
        viscosity_pa_s=liquid_viscosity_pa_s,
    )
    outside = _outside_the_fitted_bands(
        solids_loading_percent=solids_loading_percent, particle_diameter_m=particle_diameter_m
    )
    return JustSuspended(
        speed_rpm=duty.speed_rpm,
        speed_rev_per_s=speed,
        tip_speed_m_per_s=duty.tip_speed_m_per_s,
        power_w=duty.power_w,
        power_per_volume_w_per_m3=duty.power_per_volume_w_per_m3,
        reynolds_number=duty.reynolds_number,
        turbulent=duty.turbulent,
        within_fitted_range=not outside,
        outside_fitted_range=outside,
    )
