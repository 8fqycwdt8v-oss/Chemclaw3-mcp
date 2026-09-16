"""Agitation scale-up: power per volume, tip speed, and Zwietering's just-suspended speed.

Two operations that a chemist asks for in one breath and that this module keeps apart, because one
is arithmetic and the other is a fitted correlation.

**Power and tip speed are definitions.** In fully turbulent flow the ungassed power drawn by an
impeller is

    P = N_p·rho·N³·D⁵

with `N` in revolutions per second, and the tip speed is `π·N·D`. Nothing is fitted; the only
judgement is the power number `N_p`, which is the impeller's own published figure and which this
module refuses to guess. **The turbulent qualifier is load-bearing**: `N_p` is constant only above
Re ≈ 10⁴, so every answer carries the impeller Reynolds number `rho·N·D²/µ` and says which side of
that line it is on. Below it the same arithmetic returns a number that is simply wrong, and there
is no way to see that from the number.

**Zwietering's `N_js` is a correlation with a fitted exponent set**, from 1958, over sand and salt
in flat-bottomed vessels:

    N_js = S·nu^0.1·d_p^0.2·(g·(rho_s - rho_L)/rho_L)^0.45·X^0.13/D^0.85

`S` is a dimensionless geometry constant — impeller type, `T/D`, off-bottom clearance — and it is
an *input* here for the same reason `N_p` is: it is a number read from the impeller's own table,
and a default would be this module inventing the answer. The correlation predicts the speed at
which no particle rests on the base for more than one to two seconds. That is a visual criterion,
not a mass-transfer or a uniformity criterion, and the distinction is what makes "the solids are
suspended" and "the slurry is homogeneous" two different questions.

**Scale-up matches a criterion, and the criteria disagree.** Equal `P/V` and equal tip speed cannot
both be held at a new diameter, and which one to hold depends on what is limiting — dispersion and
heat transfer follow `P/V`, shear-sensitive particles and gas-liquid surfaces follow tip speed. So
this module computes both and reports the disagreement rather than choosing. What it never does is
pick one silently.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from chemclaw_mcp_unitops.engine.validation import GRAVITY_M_PER_S2, positive

__all__ = [
    "TURBULENT_REYNOLDS",
    "ZWIETERING_EXPONENTS",
    "AgitationDuty",
    "AgitationScaleUp",
    "JustSuspended",
    "agitation_scale_up",
    "just_suspended_speed",
]

#: Above this impeller Reynolds number the power number is effectively constant and the power
#: law above holds. The transitional band below it runs down to Re ≈ 10, where `N_p` rises
#: steeply and a
#: single published figure stops describing the impeller at all. Reported rather than refused: a
#: viscous slurry is a real thing to ask about, and what the caller needs is to be told the power
#: number they supplied is no longer a constant.
TURBULENT_REYNOLDS = 1.0e4

#: Zwietering's exponents, as published: kinematic viscosity, particle diameter, the buoyancy
#: group, solids loading and impeller diameter. Held as a mapping rather than inlined so
#: `engine/selftest.py` can check the *dimensional* consequence of the set — the length exponents
#: must sum to zero and the time exponents to -1 — which is a relation no line below contains and
#: which a transposed pair breaks.
ZWIETERING_EXPONENTS = {
    "kinematic_viscosity": 0.1,
    "particle_diameter": 0.2,
    "buoyancy": 0.45,
    "solids_loading": 0.13,
    "impeller_diameter": -0.85,
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
    #: How far apart the two criteria are, as the ratio of the two large-scale speeds. 1.0 would
    #: mean they agree, which happens only if the diameters are equal.
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
    power = power_number * density_kg_per_m3 * speed_rev_per_s**3 * diameter_m**5
    reynolds = density_kg_per_m3 * speed_rev_per_s * diameter_m**2 / viscosity_pa_s
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

    The speed that reproduces the small vessel's `P/V` is solved from the two vessels' **supplied**
    volumes rather than from an assumed geometric similarity:

        N₂ = [ (P/V)₁ · V₂ / (N_p · rho · D₂⁵) ]^(1/3)

    Under geometric similarity (`V ∝ D³`) that reduces to the familiar `N₂ = N₁·(D₁/D₂)^(2/3)`, and
    `engine/selftest.py` checks that it does — but a 1 L round-bottomed flask and a 250 L vessel are
    not geometrically similar, and the reduced form would quietly assume they were.

    Matching tip speed is exact and needs no volume at all: `N₂ = N₁·D₁/D₂`.

    Args:
        small_impeller_diameter_m: Impeller diameter at the small scale, in metres. The *impeller*,
            not the vessel — the two are confused often enough that the answer changes by `(T/D)⁵`.
        small_speed_rpm: Agitator speed at the small scale, in revolutions per minute.
        small_liquid_volume_m3: Liquid volume at the small scale, in m³ (1 L = 0.001 m³).
        large_impeller_diameter_m: Impeller diameter at the large scale, in metres.
        large_liquid_volume_m3: Liquid volume at the large scale, in m³.
        power_number: The impeller's turbulent power number `N_p`, dimensionless and from the
            impeller's own data. No default: a wrong `N_p` scales the whole power answer linearly.
        liquid_density_kg_per_m3: Liquid density, in kg/m³.
        liquid_viscosity_pa_s: Liquid dynamic viscosity, in Pa·s (1 cP = 0.001 Pa·s). Needed for
            the Reynolds number that says whether the power number is a constant at all.

    Returns:
        The small vessel's duty and the large vessel's under each criterion, with the ratio between
        the two large-scale speeds.

    Raises:
        UnitOpsInputError: If any dimension, speed, volume, density, viscosity or power number is
            not positive.
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
    speed_for_power = (
        wanted_power / (power_number * liquid_density_kg_per_m3 * large_impeller_diameter_m**5)
    ) ** (1.0 / 3.0)
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
    #: Ungassed power at `N_js`, from `P = N_p·rho·N³·D⁵`. Reported because the number a scale-up
    #: argument actually turns on is the P/V this implies, not the speed.
    power_w: float
    power_per_volume_w_per_m3: float
    reynolds_number: float
    turbulent: bool


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

    A **correlation with a fitted exponent set**, not a measurement and not a first-principles
    result. It was regressed over sand and sodium chloride in flat-bottomed, baffled vessels, and
    its criterion is visual: no particle stationary on the base for longer than one to two seconds.
    **The accuracy figure usually quoted for it is of the order of ±10% on its own data.**
    That is a figure taken from the literature rather than one measured here — nothing in this
    repository holds the original dataset — and it is the *best* case: a dished base, a cohesive
    or a needle-shaped solid, or a slurry outside the loading range it was fitted over can put
    a real vessel well outside it.

    Args:
        impeller_diameter_m: Impeller diameter `D`, in metres.
        particle_diameter_m: Particle diameter `d_p`, in metres. A 200 µm crystal is 2.0e-4. The
            correlation takes a single size; a distribution is usually entered at its mass-median
            diameter, and a wide distribution is one of the ways that quoted accuracy stops holding.
        particle_density_kg_per_m3: Solid density, in kg/m³. The *crystal* density, not the bulk or
            tapped density of a powder.
        liquid_density_kg_per_m3: Liquid density, in kg/m³.
        liquid_viscosity_pa_s: Liquid dynamic viscosity, in Pa·s (1 cP = 0.001 Pa·s). Converted to
            the kinematic viscosity the correlation takes.
        solids_loading_percent: `X`, 100 x (mass of solids / mass of liquid). **A percentage, and
            on a liquid basis rather than a slurry basis** — 10 kg of solid in 100 kg of solvent is
            10, not 9.09.
        zwietering_constant: `S`, the dimensionless geometry constant for this impeller type,
            `T/D` ratio and off-bottom clearance, from Zwietering's own table or the impeller
            vendor's. No default: `S` spans roughly 2 to 15 across ordinary geometries, and
            assuming one would be this correlation's entire answer.
        power_number: The impeller's turbulent power number, for the power at `N_js`.
        liquid_volume_m3: Liquid volume, in m³, for the P/V at `N_js`.

    Returns:
        The just-suspended speed and what running there costs in power and tip speed.

    Raises:
        UnitOpsInputError: If any input is not positive. A zero density difference is refused by
            name, because a neutrally buoyant solid has no just-suspended speed — it does not
            settle — and the correlation would return zero, which reads as "no agitation needed".
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
    speed = (
        zwietering_constant
        * kinematic_viscosity ** ZWIETERING_EXPONENTS["kinematic_viscosity"]
        * particle_diameter_m ** ZWIETERING_EXPONENTS["particle_diameter"]
        * buoyancy ** ZWIETERING_EXPONENTS["buoyancy"]
        * solids_loading_percent ** ZWIETERING_EXPONENTS["solids_loading"]
        * impeller_diameter_m ** ZWIETERING_EXPONENTS["impeller_diameter"]
    )
    duty = _duty(
        speed_rev_per_s=speed,
        diameter_m=impeller_diameter_m,
        volume_m3=liquid_volume_m3,
        power_number=power_number,
        density_kg_per_m3=liquid_density_kg_per_m3,
        viscosity_pa_s=liquid_viscosity_pa_s,
    )
    return JustSuspended(
        speed_rpm=duty.speed_rpm,
        speed_rev_per_s=speed,
        tip_speed_m_per_s=duty.tip_speed_m_per_s,
        power_w=duty.power_w,
        power_per_volume_w_per_m3=duty.power_per_volume_w_per_m3,
        reynolds_number=duty.reynolds_number,
        turbulent=duty.turbulent,
    )
