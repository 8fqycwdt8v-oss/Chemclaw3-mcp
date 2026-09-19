"""The `unitops` MCP tool surface: the sizing arithmetic between a route on paper and a plant batch.

**These docstrings are the prompt**, and what they have to get across here is narrower than the
server's name suggests. Every tool takes **equipment and material data the chemist already has** —
an impeller diameter, a measured heat-transfer coefficient, a relative volatility, two points on a
solubility curve, a specific cake resistance, a drying rate — and returns what a standard
correlation makes of them.

**This server holds no data at all.** No vessel register, no VLE table, no solubility curve, no
cake resistance, no drying curve, no impeller catalogue. That is the whole of what separates a
useful answer here from a fabricated one: every tool refuses to default the number its answer is
made of, and several of them will not run without it. A question of the form "how long will the
filtration take on the 30-inch Nutsche" cannot be answered from the vessel alone — it needs a
filtration test on *this* slurry — and the honest response is to say which measurement is missing.

**Nothing here is a simulation.** A shortcut column is not a stage-by-stage calculation; a
Zwietering `N_js` is a correlation with a fitted geometry constant, quoted at roughly ±10% on its
own data; a crystallisation yield is an equilibrium mass balance and a real batch filters out of a
supersaturated liquor; a cake filtration assumes an incompressible cake, which fine organic solids
routinely are not. Every answer carries `basis`: the equation it came out of and the assumption
that equation makes.

**Two boundaries worth stating because the tools sit next to them.**

`heat_transfer_time_constant` returns a *capacity* — how fast this jacket can move heat at a stated
driving force. It is not a heat **load**, which comes from calorimetry, and putting the two together
is a thermal-safety question that belongs to `servers/thermalsafety`. Nothing here returns a TMR, an
MTSR or a criticality class.

`crystallisation_yield` takes solubilities as inputs and predicts none. Chemclaw3's own solubility
predictor is aqueous, neutral-species and temperature-free, so it does not answer "how soluble is
this in isopropanol at 5 °C" and a number borrowed from it would be the near-miss rather than the
answer.

The tools are synchronous and closed-form. The widest is a 200-step bisection for Underwood's root,
which is a bound on the cost rather than an adaptive controller.
"""

from __future__ import annotations

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from chemclaw_mcp_unitops.engine import (
    crystallisation,
    distillation,
    drying,
    filtration,
    heat,
    mixing,
)

server = FastMCP("unitops")


class VesselDuty(BaseModel):
    """One vessel's agitation duty at one speed."""

    speed_rpm: float = Field(description="Agitator speed, in revolutions per minute.")
    tip_speed_m_per_s: float = Field(description="Impeller tip speed pi*N*D, in m/s.")
    power_w: float = Field(
        description="Ungassed impeller power N_p*rho*N^3*D^5, in W. Turbulent flow only."
    )
    power_per_volume_w_per_m3: float = Field(
        description="Power per unit liquid volume, in W/m3. 1 W/L is 1000 W/m3."
    )
    reynolds_number: float = Field(
        description=(
            "Impeller Reynolds number rho*N*D^2/mu, dimensionless. Below about 1e4 the power "
            "number is no longer a constant and the power figure beside it is not reliable."
        )
    )
    turbulent: bool = Field(
        description="True when the Reynolds number is at or above 1e4, where N_p is constant."
    )


class AgitationScaleUpResult(BaseModel):
    """The same duty carried to a second vessel under each of the two usual criteria."""

    small: VesselDuty
    matched_power_per_volume: VesselDuty = Field(
        description="The large vessel run at the speed that reproduces the small vessel's P/V."
    )
    matched_tip_speed: VesselDuty = Field(
        description=(
            "The large vessel run at the speed that reproduces the small vessel's tip speed."
        )
    )
    criteria_disagree_by: float = Field(
        description=(
            "The ratio of the two large-scale speeds. Above 1 the P/V criterion asks for the "
            "faster agitator; the gap is why scale-up needs a criterion chosen rather than "
            "assumed."
        )
    )
    basis: str


class JustSuspendedResult(BaseModel):
    """Zwietering's just-suspended speed and what running there costs."""

    speed_rpm: float = Field(description="N_js, in revolutions per minute.")
    speed_rev_per_s: float = Field(description="N_js, in revolutions per second.")
    tip_speed_m_per_s: float
    power_w: float = Field(description="Ungassed power at N_js, in W.")
    power_per_volume_w_per_m3: float = Field(description="P/V at N_js, in W/m3.")
    reynolds_number: float
    turbulent: bool
    within_fitted_range: bool = Field(
        description=(
            "Whether the solids loading and particle size sit inside the bands Zwietering "
            "regressed over. False means N_js is an extrapolation; see within_fitted_range_notes."
        )
    )
    within_fitted_range_notes: list[str] = Field(
        description=(
            "One sentence per input outside its fitted band, naming the input, its value and the "
            "band. Empty when within_fitted_range is true."
        )
    )
    basis: str


class HeatTransferResult(BaseModel):
    """A jacketed vessel's first-order thermal response."""

    time_constant_seconds: float = Field(description="tau = M*cp/(U*A), in seconds.")
    time_constant_minutes: float
    initial_duty_w: float = Field(
        description=(
            "U*A*(T_jacket - T_batch) at the START, in W, signed so a cool-down is negative. It is "
            "the LARGEST the duty will be and it falls as the batch approaches the jacket. This is "
            "a capacity, not a heat load: a reaction's heat output comes from calorimetry."
        )
    )
    approach_at_one_time_constant: float = Field(
        description=(
            "0.632 — the fraction of the gap closed at t = tau, for every first-order system."
        )
    )
    temperature_after_c: float | None = Field(
        default=None, description="Batch temperature after the time asked for, in degrees Celsius."
    )
    time_to_target_seconds: float | None = Field(
        default=None, description="Time to reach the target temperature asked for, in seconds."
    )
    time_to_target_minutes: float | None = None
    basis: str


class ShortcutDistillationResult(BaseModel):
    """A shortcut column: the two bounds and the stage count between them."""

    minimum_stages: float = Field(
        description=(
            "Fenske's N_min: THEORETICAL stages at total reflux, INCLUDING the reboiler. Divide by "
            "a measured tray efficiency to get actual trays; this server holds no efficiency."
        )
    )
    minimum_reflux_ratio: float = Field(
        description="Underwood's R_min. Below it the split is unreachable at any stage count."
    )
    underwood_theta: float = Field(
        description="Underwood's root, which lies between 1 and the relative volatility."
    )
    reflux_ratio: float = Field(description="The reflux ratio L/D the stage count was computed at.")
    reflux_over_minimum: float = Field(description="R/R_min, the number a design is argued over.")
    theoretical_stages: float = Field(
        description=(
            "Gilliland's N at that reflux, including the reboiler. An EMPIRICAL correlation: +-10 "
            "to 20% on N is ordinary, and it is at its worst close to R_min."
        )
    )
    stages_above_minimum: float
    basis: str


class CrystallisationYieldResult(BaseModel):
    """The maximum yield two solubilities permit."""

    crystal_mass_kg: float
    mother_liquor_loss_kg: float = Field(
        description="Product still dissolved when the liquor is filtered off, in kg."
    )
    yield_fraction: float
    yield_percent: float
    solvent_at_end_kg: float
    saturation_at_start: float = Field(
        description=(
            "Solute charged divided by what the hot solvent can hold. 1.0 is exactly saturated; "
            "well below it, the batch is too dilute to yield well before it is cooled at all."
        )
    )
    basis: str


class FiltrationTimeResult(BaseModel):
    """A constant-pressure cake filtration, split into its two resistances."""

    total_time_seconds: float
    total_time_minutes: float
    total_time_hours: float
    cake_time_seconds: float = Field(description="The cake's share — the term quadratic in volume.")
    medium_time_seconds: float = Field(description="The cloth's share — the term linear in volume.")
    cake_fraction_of_time: float = Field(
        description=(
            "The cake's share of the total, 0 to 1. Near 1 the cloth is irrelevant and a slow "
            "filtration is a crystallisation problem; nearer 0.5 the medium is worth looking at."
        )
    )
    average_flux_m3_per_m2_h: float
    final_rate_m3_per_s: float = Field(
        description="Instantaneous filtrate rate as the last of it passes — the slowest it gets."
    )
    cake_mass_kg: float
    basis: str


class DryingTimeResult(BaseModel):
    """A batch dry, split into its constant-rate and falling-rate periods."""

    constant_rate_seconds: float
    falling_rate_seconds: float
    total_time_seconds: float
    total_time_hours: float
    moisture_removed_kg: float
    starts_in_the_falling_rate_period: bool = Field(
        description=(
            "True when the charge is already below the critical moisture, so there is no "
            "constant-rate period and the whole cycle runs on the slower falling-rate leg."
        )
    )
    basis: str


def _duty(source: mixing.AgitationDuty) -> VesselDuty:
    """One engine duty as the model sees it."""
    return VesselDuty(
        speed_rpm=source.speed_rpm,
        tip_speed_m_per_s=source.tip_speed_m_per_s,
        power_w=source.power_w,
        power_per_volume_w_per_m3=source.power_per_volume_w_per_m3,
        reynolds_number=source.reynolds_number,
        turbulent=source.turbulent,
    )


@server.tool()
def agitation_scale_up(
    small_impeller_diameter_m: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "IMPELLER diameter at the small scale, in metres — not the vessel diameter. The "
                "power goes as the fifth power of it, so confusing the two is a factor of hundreds."
            ),
        ),
    ],
    small_speed_rpm: Annotated[
        float, Field(gt=0, description="Agitator speed at the small scale, in rpm.")
    ],
    small_liquid_volume_m3: Annotated[
        float, Field(gt=0, description="Liquid volume at the small scale, in m3 (1 L = 0.001 m3).")
    ],
    large_impeller_diameter_m: Annotated[
        float, Field(gt=0, description="IMPELLER diameter at the large scale, in metres.")
    ],
    large_liquid_volume_m3: Annotated[
        float, Field(gt=0, description="Liquid volume at the large scale, in m3.")
    ],
    power_number: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "The impeller's turbulent power number N_p, dimensionless, from the impeller's own "
                "data. No default: it scales the whole power answer linearly. A Rushton turbine is "
                "around 5 and a pitched-blade turbine around 1.3-1.5, but use the real figure — "
                "N_p depends on the baffling and the clearance as well as the blade."
            ),
        ),
    ],
    liquid_density_kg_per_m3: Annotated[
        float, Field(gt=0, description="Liquid density, in kg/m3. Water is 1000.")
    ],
    liquid_viscosity_pa_s: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "Liquid dynamic viscosity, in Pa*s (1 cP = 0.001 Pa*s). Water is 0.001, toluene "
                "0.00056, IPA 0.0021. Needed for the Reynolds number that says whether the power "
                "number is a constant at all."
            ),
        ),
    ],
) -> AgitationScaleUpResult:
    """Carry an agitation duty from one vessel to another: P/V, tip speed, and both matched speeds.

    Answers "what speed do I run the big vessel at" — under **both** of the criteria a process
    chemist would match on, because they disagree and the tool will not choose for you. Matching
    `P/V` keeps the energy dissipation per unit volume, which is what dispersion, mass transfer and
    heat transfer follow; matching **tip speed** keeps the maximum shear, which is what a fragile
    crystal or a gas-liquid surface follows. Scaled up, the P/V criterion always asks for the higher
    tip speed and the tip-speed criterion always gives the lower P/V, and the returned
    `criteria_disagree_by` says by how much.

    **This is not a suspension calculation.** Whether solids stay off the base is Zwietering's
    `N_js` and a different tool (`just_suspended_speed`), because it needs the particle size and
    density this one never asks for.

    **What it assumes.** Fully turbulent, ungassed, single-phase flow with a constant power number.
    The Reynolds number comes back with every answer and says whether that holds. It knows nothing
    about baffling, impeller clearance, vessel shape, draw-off, gassing or a second impeller.

    Args:
        small_impeller_diameter_m: Small-scale impeller diameter, m.
        small_speed_rpm: Small-scale agitator speed, rpm.
        small_liquid_volume_m3: Small-scale liquid volume, m3.
        large_impeller_diameter_m: Large-scale impeller diameter, m.
        large_liquid_volume_m3: Large-scale liquid volume, m3.
        power_number: The impeller's turbulent power number, dimensionless.
        liquid_density_kg_per_m3: Liquid density, kg/m3.
        liquid_viscosity_pa_s: Liquid dynamic viscosity, Pa*s.

    Returns:
        The small vessel's duty and the large vessel's under each criterion.

    Raises:
        ValueError: If any diameter, speed, volume, density, viscosity or power number is not
            positive.
    """
    scaled = mixing.agitation_scale_up(
        small_impeller_diameter_m=small_impeller_diameter_m,
        small_speed_rpm=small_speed_rpm,
        small_liquid_volume_m3=small_liquid_volume_m3,
        large_impeller_diameter_m=large_impeller_diameter_m,
        large_liquid_volume_m3=large_liquid_volume_m3,
        power_number=power_number,
        liquid_density_kg_per_m3=liquid_density_kg_per_m3,
        liquid_viscosity_pa_s=liquid_viscosity_pa_s,
    )
    return AgitationScaleUpResult(
        small=_duty(scaled.small),
        matched_power_per_volume=_duty(scaled.matched_power_per_volume),
        matched_tip_speed=_duty(scaled.matched_tip_speed),
        criteria_disagree_by=scaled.criteria_disagree_by,
        basis=(
            "turbulent ungassed impeller power P = N_p*rho*N^3*D^5 and tip speed pi*N*D, matched "
            "on the supplied volumes rather than on assumed geometric similarity; constant power "
            "number, single phase, no gassing, and nothing about baffling or clearance"
        ),
    )


@server.tool()
def just_suspended_speed(
    impeller_diameter_m: Annotated[
        float, Field(gt=0, description="IMPELLER diameter D, in metres.")
    ],
    particle_diameter_m: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "Particle diameter d_p, in METRES — a 200 micron crystal is 2e-4. A single size; "
                "for a distribution the mass-median diameter is what is normally entered, and a "
                "wide distribution is one of the ways this correlation's accuracy stops holding."
            ),
        ),
    ],
    particle_density_kg_per_m3: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "True CRYSTAL density of the solid, in kg/m3 — not the bulk or tapped density of "
                "a powder, which is lower by the void fraction. Most organic solids are 1200-1600."
            ),
        ),
    ],
    liquid_density_kg_per_m3: Annotated[
        float, Field(gt=0, description="Liquid density, in kg/m3.")
    ],
    liquid_viscosity_pa_s: Annotated[
        float, Field(gt=0, description="Liquid dynamic viscosity, in Pa*s (1 cP = 0.001 Pa*s).")
    ],
    solids_loading_percent: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "X = 100 * (mass of solids / mass of LIQUID). A PERCENTAGE, and on a liquid basis "
                "rather than a slurry basis: 10 kg of solid in 100 kg of solvent is 10, not 9.09."
            ),
        ),
    ],
    zwietering_constant: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "S, the dimensionless geometry constant for this impeller type, vessel-to-impeller "
                "diameter ratio and off-bottom clearance, from Zwietering's table or the impeller "
                "vendor's data. No default: S spans roughly 2 to 15 across ordinary geometries, so "
                "assuming one would be inventing the whole answer. If you do not have S, say so — "
                "the speed cannot be computed without it."
            ),
        ),
    ],
    power_number: Annotated[
        float,
        Field(gt=0, description="The impeller's turbulent power number, for the P/V at N_js."),
    ],
    liquid_volume_m3: Annotated[
        float, Field(gt=0, description="Liquid volume, in m3, for the P/V at N_js.")
    ],
) -> JustSuspendedResult:
    """Zwietering's just-suspended speed: the agitation at which no solid rests on the vessel base.

    **A 1958 correlation with fitted exponents, not a measurement and not a derivation.** It was
    regressed over sand and sodium chloride in flat-bottomed baffled vessels, and its criterion is
    visual — no particle stationary on the base for more than one to two seconds. On its own data it
    is quoted at roughly ±10% — a literature figure, not one measured here. A dished base, a
    cohesive or needle-shaped solid, a wide size distribution, or a loading outside the fitted
    range can put a real vessel well outside that.

    **So the fitted range is checked and reported.** `within_fitted_range` is false, with a sentence
    naming the input, whenever the solids loading or the particle size is outside the band the
    correlation was regressed over. `X^0.13` is a weak exponent, so a loading entered as a fraction
    where a percentage is asked for returns a perfectly plausible speed: measured, 10 wt% entered as
    0.10 gives an N_js 45.05% low and a P/V 83.40% low, which is an under-agitated vessel. Reported
    rather than refused, because the correlation is routinely used a little outside its regression.

    **Just-suspended is not homogeneous.** `N_js` is the speed at which nothing sits still on the
    bottom; it is not the speed at which the slurry is uniform up the vessel, and a sample taken
    from a side port at `N_js` will be lean. If the question is about a representative sample or an
    even draw-off, this is the wrong number.

    It also says nothing about **attrition**: running well above `N_js` breaks crystals, which is
    the usual reason a filtration slows down, so more agitation is not free.

    Args:
        impeller_diameter_m: Impeller diameter, m.
        particle_diameter_m: Particle diameter, m.
        particle_density_kg_per_m3: Crystal density, kg/m3.
        liquid_density_kg_per_m3: Liquid density, kg/m3.
        liquid_viscosity_pa_s: Liquid dynamic viscosity, Pa*s.
        solids_loading_percent: Solids as a percentage of the liquid mass.
        zwietering_constant: The geometry constant S, dimensionless.
        power_number: The impeller's turbulent power number.
        liquid_volume_m3: Liquid volume, m3.

    Returns:
        The just-suspended speed, and the tip speed and P/V that running there implies.

    Raises:
        ValueError: If any input is not positive, or the solid is not denser than the liquid — a
            neutrally buoyant or floating solid has no just-suspended speed, and this correlation
            does not describe one.
    """
    found = mixing.just_suspended_speed(
        impeller_diameter_m=impeller_diameter_m,
        particle_diameter_m=particle_diameter_m,
        particle_density_kg_per_m3=particle_density_kg_per_m3,
        liquid_density_kg_per_m3=liquid_density_kg_per_m3,
        liquid_viscosity_pa_s=liquid_viscosity_pa_s,
        solids_loading_percent=solids_loading_percent,
        zwietering_constant=zwietering_constant,
        power_number=power_number,
        liquid_volume_m3=liquid_volume_m3,
    )
    return JustSuspendedResult(
        speed_rpm=found.speed_rpm,
        speed_rev_per_s=found.speed_rev_per_s,
        tip_speed_m_per_s=found.tip_speed_m_per_s,
        power_w=found.power_w,
        power_per_volume_w_per_m3=found.power_per_volume_w_per_m3,
        reynolds_number=found.reynolds_number,
        turbulent=found.turbulent,
        within_fitted_range=found.within_fitted_range,
        within_fitted_range_notes=list(found.outside_fitted_range),
        basis=(
            "Zwietering (1958) N_js = S*nu^0.1*d_p^0.2*(g*drho/rho)^0.45*X^0.13/D^0.85, an "
            "empirical correlation with a fitted geometry constant, regressed on sand and salt in "
            "flat-bottomed baffled vessels; the criterion is no particle at rest on the base for "
            "more than 1-2 s, which is not slurry homogeneity"
        ),
    )


@server.tool()
def heat_transfer_time_constant(
    batch_mass_kg: Annotated[float, Field(gt=0, description="Mass of the batch contents, in kg.")],
    heat_capacity_j_per_kg_k: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "Specific heat capacity of the contents, in J/(kg*K). Water is 4180; most organic "
                "solvents are 1600-2200."
            ),
        ),
    ],
    overall_heat_transfer_coefficient_w_per_m2_k: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "U, in W/(m2*K), from a MEASURED characterisation of this vessel. There is no "
                "default and no correlation behind it: U depends on the service, the agitation, "
                "the fouling and the wall, and an assumed U is the whole of the answer. If you do "
                "not have one, say so rather than guessing — a computed duty from a guessed U is "
                "the most dangerous shape this question has, because it looks like engineering."
            ),
        ),
    ],
    heat_transfer_area_m2: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "The WETTED jacket area at this fill, in m2. A part-filled vessel has less area "
                "than its jacket, and using the full jacket area overstates the duty."
            ),
        ),
    ],
    initial_temperature_c: Annotated[
        float, Field(description="Batch temperature at the start, in degrees Celsius.")
    ],
    jacket_temperature_c: Annotated[
        float, Field(description="Jacket service temperature, in degrees Celsius, held constant.")
    ],
    target_temperature_c: Annotated[
        float | None,
        Field(
            default=None,
            description=(
                "Optional: a batch temperature to reach, in degrees Celsius. Must lie strictly "
                "between the starting and the jacket temperature — the jacket temperature itself "
                "is approached asymptotically, so asking for it is asking for infinite time."
            ),
        ),
    ] = None,
    time_seconds: Annotated[
        float | None,
        Field(
            default=None,
            description="Optional: a time in seconds, to get the batch temperature there.",
        ),
    ] = None,
) -> HeatTransferResult:
    """How fast a jacketed batch heats or cools: tau = M*cp/(U*A), and the duty at the starting gap.

    A perfectly mixed batch against a constant jacket approaches it exponentially, so one number —
    the time constant — describes the whole transient: 63% of the gap closed at `t = tau`, 86% at
    `2*tau`, 95% at `3*tau`. **This is the number that scales badly**: mass grows with volume and
    area with surface, so a cool-down that took twenty minutes at 1 L takes hours at 250 L, which is
    one of the main reasons a lab recipe behaves differently in a plant.

    **`initial_duty_w` is a CAPACITY, not a heat load.** It is how fast this jacket can move heat at
    the starting temperature difference, and it falls as the batch approaches the jacket. Comparing
    it with a reaction's heat output needs that heat output, which comes from **calorimetry** and
    exists nowhere in this system. Whether a reactor can take a reaction's duty is a thermal-safety
    question for `servers/thermalsafety` given measured data, and answering it from an assumed U and
    an assumed enthalpy would be a fabrication in the shape of an engineering calculation.

    **What it assumes.** Perfectly mixed batch, constant `U`, constant `cp`, constant jacket
    temperature, no reaction heat, no phase change, no heat loss to ambient, no jacket-side
    dynamics and no fouling over the cycle. A real jacket ramping its own setpoint, a reflux, or a
    reaction running while the batch cools are all outside it.

    Args:
        batch_mass_kg: Contents mass, kg.
        heat_capacity_j_per_kg_k: Contents specific heat capacity, J/(kg*K).
        overall_heat_transfer_coefficient_w_per_m2_k: Measured U, W/(m2*K).
        heat_transfer_area_m2: Wetted jacket area at this fill, m2.
        initial_temperature_c: Starting batch temperature, degrees Celsius.
        jacket_temperature_c: Jacket temperature, degrees Celsius.
        target_temperature_c: Optional target, degrees Celsius.
        time_seconds: Optional time, seconds.

    Returns:
        The time constant, the duty at the starting gap, and whichever inversion was asked for.

    Raises:
        ValueError: If a mass, capacity, coefficient or area is not positive, a temperature is
            below absolute zero, the batch already sits at the jacket temperature, or a target lies
            outside the gap between the two.
    """
    transient = heat.time_constant(
        batch_mass_kg=batch_mass_kg,
        heat_capacity_j_per_kg_k=heat_capacity_j_per_kg_k,
        overall_heat_transfer_coefficient_w_per_m2_k=overall_heat_transfer_coefficient_w_per_m2_k,
        heat_transfer_area_m2=heat_transfer_area_m2,
        initial_temperature_c=initial_temperature_c,
        jacket_temperature_c=jacket_temperature_c,
        target_temperature_c=target_temperature_c,
        time_seconds=time_seconds,
    )
    return HeatTransferResult(
        time_constant_seconds=transient.time_constant_seconds,
        time_constant_minutes=transient.time_constant_minutes,
        initial_duty_w=transient.initial_duty_w,
        approach_at_one_time_constant=transient.approach_at_one_time_constant,
        temperature_after_c=transient.temperature_after_c,
        time_to_target_seconds=transient.time_to_target_seconds,
        time_to_target_minutes=transient.time_to_target_minutes,
        basis=(
            "lumped first-order energy balance M*cp*dT/dt = U*A*(T_jacket - T), giving "
            "tau = M*cp/(U*A); perfectly mixed batch, constant U and jacket temperature, no "
            "reaction heat, no phase change and no ambient loss. The duty is a capacity at the "
            "stated driving force, not a process heat load"
        ),
    )


@server.tool()
def shortcut_distillation(
    relative_volatility: Annotated[
        float,
        Field(
            gt=1,
            description=(
                "alpha of the light key over the heavy key, dimensionless and above 1, taken as "
                "CONSTANT over the whole column. From a measured x-y curve, a simulation or a "
                "literature value at the column's pressure. A ratio of boiling points is NOT "
                "alpha, and this server holds no VLE data to derive one."
            ),
        ),
    ],
    light_key_in_feed: Annotated[
        float,
        Field(
            gt=0,
            lt=1,
            description="Mole fraction of the light key in the feed. 0.5 for equimolar, not 50.",
        ),
    ],
    light_key_in_distillate: Annotated[
        float, Field(gt=0, lt=1, description="Mole fraction of the light key in the distillate.")
    ],
    light_key_in_bottoms: Annotated[
        float, Field(gt=0, lt=1, description="Mole fraction of the light key in the bottoms.")
    ],
    reflux_ratio: Annotated[
        float | None,
        Field(
            default=None,
            description=(
                "Optional: the reflux ratio L/D to evaluate at. Must exceed the computed minimum. "
                "When omitted, `reflux_over_minimum` times the minimum is used instead."
            ),
        ),
    ] = None,
    reflux_over_minimum: Annotated[
        float,
        Field(
            default=1.3,
            gt=1,
            description=(
                "The multiple of R_min to design at when no absolute reflux ratio is given. 1.3 is "
                "the middle of the band real columns sit in (1.05-1.5); it is a CONVENTION, and "
                "the answer says which reflux was actually used."
            ),
        ),
    ] = 1.3,
    feed_quality: Annotated[
        float,
        Field(
            default=1.0,
            description=(
                "q, the feed's thermal condition: 1 for a saturated liquid (the usual case), 0 for "
                "a saturated vapour, above 1 subcooled, below 0 superheated."
            ),
        ),
    ] = 1.0,
) -> ShortcutDistillationResult:
    """Fenske-Underwood-Gilliland: minimum stages, minimum reflux, and stages at a chosen reflux.

    The screening arithmetic for "how big a column does this separation need". Three results that
    belong together: Fenske's `N_min` at total reflux, Underwood's `R_min` below which no column
    works, and Gilliland's estimate of the stages between the two.

    **This is not a column simulation and not a solvent-swap calculation.** It has no VLE data, no
    stage-by-stage material balance, no tray hydraulics and no temperature profile. It assumes a
    **single constant relative volatility** over the whole column, which is false for an azeotropic
    or strongly non-ideal pair and approximate for any column whose ends are at very different
    temperatures. It returns **theoretical** stages including the reboiler; actual trays need a
    measured efficiency this server does not hold.

    **It cannot tell you a residual solvent level in ppm.** A DMF-to-2-MeTHF swap asks how much DMF
    survives, and that is set by the number of volume turnovers in a distillative swap and by the
    analysis afterwards — not by a stage count. A specification in ppm from this tool would be a
    number with nothing behind it.

    Args:
        relative_volatility: alpha of light key over heavy key, constant, above 1.
        light_key_in_feed: Light-key mole fraction in the feed. Must lie strictly between the
            bottoms and the distillate: a feed no richer than the bottoms cannot be split into two
            products that are both richer than it is, and the tool refuses rather than answering.
        light_key_in_distillate: Light-key mole fraction in the distillate.
        light_key_in_bottoms: Light-key mole fraction in the bottoms.
        reflux_ratio: Optional absolute reflux ratio L/D.
        reflux_over_minimum: Multiple of R_min to use when no absolute reflux is given.
        feed_quality: q, the feed's thermal condition.

    Returns:
        The minimum stages, the minimum reflux, Underwood's root, and the stages at the reflux used.

    Raises:
        ValueError: If a composition is outside (0, 1), the distillate is not richer in the light
            key than the feed, the bottoms are not leaner than the distillate, or the reflux ratio
            is at or below the minimum — where the stage count is infinite rather than large. An
            alpha at or below 1 is refused by this argument's own schema bound rather than by the
            body, so the refusal names the argument instead of explaining the azeotrope; the
            description above is where that explanation is.
    """
    column = distillation.shortcut_column(
        relative_volatility=relative_volatility,
        light_key_in_feed=light_key_in_feed,
        light_key_in_distillate=light_key_in_distillate,
        light_key_in_bottoms=light_key_in_bottoms,
        reflux_ratio=reflux_ratio,
        reflux_over_minimum=reflux_over_minimum,
        feed_quality=feed_quality,
    )
    return ShortcutDistillationResult(
        minimum_stages=column.minimum_stages,
        minimum_reflux_ratio=column.minimum_reflux_ratio,
        underwood_theta=column.underwood_theta,
        reflux_ratio=column.reflux_ratio,
        reflux_over_minimum=column.reflux_over_minimum,
        theoretical_stages=column.theoretical_stages,
        stages_above_minimum=column.stages_above_minimum,
        basis=(
            "Fenske at total reflux and Underwood's minimum reflux, both exact for a binary key "
            "pair at constant relative volatility, with Gilliland's EMPIRICAL correlation "
            "(Molokanov's form) between them; theoretical stages including the reboiler, no VLE "
            "data, no tray efficiency, no hydraulics, and a single alpha assumed over the column"
        ),
    )


@server.tool()
def crystallisation_yield(
    solute_charged_kg: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "Product dissolved at the hot temperature, in kg. The solute itself, not the "
                "slurry mass and not a crude charge including impurities."
            ),
        ),
    ],
    solvent_charged_kg: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "Solvent in the vessel at the hot temperature, in kg. MASS, not volume: 5 L of "
                "isopropanol is 3.93 kg, and entering the litres overstates the liquor loss by a "
                "quarter."
            ),
        ),
    ],
    solubility_hot_kg_per_kg_solvent: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "MEASURED solubility at the starting temperature, in kg of solute per kg of "
                "solvent, in this solvent system. There is no solubility model here and no curve "
                "to look it up in."
            ),
        ),
    ],
    solubility_cold_kg_per_kg_solvent: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "MEASURED solubility at the final temperature, same units. The yield is almost "
                "entirely made of this number, so a guess here is a guess wearing a percentage."
            ),
        ),
    ],
    solvent_evaporated_kg: Annotated[
        float,
        Field(
            default=0.0,
            ge=0,
            description=(
                "Solvent removed during the operation, in kg. Zero for a straight cooling "
                "crystallisation; non-zero for concentrate-and-cool or a distillative swap."
            ),
        ),
    ] = 0.0,
) -> CrystallisationYieldResult:
    """The maximum crystallisation yield two measured solubilities permit, by mass balance.

    What crystallises is what the cold liquor cannot hold: `crystals = charged - S_cold * solvent`.
    The answer is arithmetic on the two solubilities **supplied**, and they are its entire content.

    **It is a MAXIMUM, and a real batch comes in under it.** Four reasons, each of which this
    balance cannot see:

    - The liquor is filtered **supersaturated**, not at equilibrium — a cooling crystallisation
      stops somewhere in the metastable zone, and how far depends on cooling rate, seeding and
      agitation. Real yields commonly land a few points below this.
    - A **hydrate or solvate** takes solvent out of the liquor and adds its own mass to the cake,
      so both sides of the balance move.
    - **Impurities change the solubility**, often substantially, and the curve measured on pure
      material is not the curve the batch sees.
    - Oiling out, inclusion, losses to the vessel and the wash are all outside it.

    **There is no solubility model anywhere in this system.** Chemclaw3's own predictor is aqueous,
    neutral-species and temperature-free, so it cannot supply a solubility in isopropanol at 5 °C
    and a number borrowed from it is the near-miss rather than the answer. If the two solubilities
    are not measured, this tool has nothing to work with and saying so is the answer.

    Args:
        solute_charged_kg: Product dissolved at the hot temperature, kg.
        solvent_charged_kg: Solvent charged, kg.
        solubility_hot_kg_per_kg_solvent: Measured solubility hot, kg/kg solvent.
        solubility_cold_kg_per_kg_solvent: Measured solubility cold, kg/kg solvent.
        solvent_evaporated_kg: Solvent removed during the operation, kg.

    Returns:
        The crystal mass, the mother-liquor loss, the yield, and how saturated the charge started.

    Raises:
        ValueError: If a mass or solubility is not positive, if the cold solubility is not below
            the hot one, if evaporation removes all the solvent, or if the charge exceeds what the
            hot solvent can dissolve — in which case part of it never went into solution and this
            balance describes a different experiment.
    """
    found = crystallisation.crystallisation_yield(
        solute_charged_kg=solute_charged_kg,
        solvent_charged_kg=solvent_charged_kg,
        solubility_hot_kg_per_kg_solvent=solubility_hot_kg_per_kg_solvent,
        solubility_cold_kg_per_kg_solvent=solubility_cold_kg_per_kg_solvent,
        solvent_evaporated_kg=solvent_evaporated_kg,
    )
    return CrystallisationYieldResult(
        crystal_mass_kg=found.crystal_mass_kg,
        mother_liquor_loss_kg=found.mother_liquor_loss_kg,
        yield_fraction=found.yield_fraction,
        yield_percent=found.yield_percent,
        solvent_at_end_kg=found.solvent_at_end_kg,
        saturation_at_start=found.saturation_at_start,
        basis=(
            "equilibrium mass balance on the supplied solubilities: crystals = solute charged - "
            "S_cold * solvent remaining. An anhydrous solid, equilibrium at the final temperature, "
            "no impurity effect on solubility and no losses to oiling, inclusion or wash - so it "
            "is the MAXIMUM the two solubilities permit rather than a predicted batch yield"
        ),
    )


@server.tool()
def filtration_time(
    filtrate_volume_m3: Annotated[
        float, Field(gt=0, description="Filtrate to be collected, in m3 (100 L = 0.1 m3).")
    ],
    filter_area_m2: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "Filtration AREA, in m2 — not the diameter. A 30-inch Nutsche is about 0.46 m2, "
                "and the time goes as the square of this."
            ),
        ),
    ],
    pressure_drop_pa: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "Pressure difference across cake and medium, in Pa. 1 bar = 1e5 Pa; a full vacuum "
                "gives at most about 1e5 Pa of driving force and in practice less."
            ),
        ),
    ],
    filtrate_viscosity_pa_s: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "Viscosity of the FILTRATE (the mother liquor that flows), in Pa*s. 1 cP = 0.001."
            ),
        ),
    ],
    specific_cake_resistance_m_per_kg: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "alpha, in m/kg, from a leaf or filtration test on THIS slurry at THIS pressure. "
                "Ordinary organic cakes span 1e9 (coarse, free-filtering) to 1e13 and beyond (fine "
                "or gelatinous), so it decides the answer and has no default. It is a property of "
                "the crystals - habit, size, how the batch was cooled - not of the compound, which "
                "is why the same product can filter in 40 minutes one week and six hours the next."
            ),
        ),
    ],
    dry_cake_per_filtrate_kg_per_m3: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "c, kg of dry cake deposited per m3 of filtrate collected. For a dilute slurry "
                "this is close to the solids concentration in the feed."
            ),
        ),
    ],
    medium_resistance_per_m: Annotated[
        float,
        Field(
            default=0.0,
            ge=0,
            description=(
                "R_m, the filter cloth's own resistance, in 1/m. Defaults to 0, giving the "
                "cake-only answer - an honest lower bound rather than an invented cloth. The "
                "returned split says how much of the time it accounted for."
            ),
        ),
    ] = 0.0,
) -> FiltrationTimeResult:
    """Time to filter a volume at constant pressure, and whether the cake or the cloth is limiting.

    Integrating Darcy's law through a growing cake gives a time quadratic in the filtrate volume
    plus a linear term for the medium. The **split** is the useful part: a filtration that is 99%
    cake is fixed in the crystalliser, not on the filter.

    **The cake is assumed INCOMPRESSIBLE**, and that is the assumption most likely to be wrong on
    a real plant filter. A fine, gelatinous or needle-like organic cake compresses, so `alpha` rises
    with pressure and pushing harder buys less than this predicts — sometimes nothing at all. There
    is no compressibility exponent here because fitting one needs filtration tests at several
    pressures.

    **`alpha` and `R_m` are measurements, and nothing in this system holds them.** They belong to
    the crystals produced by a particular batch, so a filtration that took six hours when it used to
    take forty minutes is asking about the crystallisation that made those crystals — which is a
    question for the batch record rather than for this arithmetic.

    **This tool does NOT compute a wash.** No wash volume, no number of displacements, no cake
    moisture, no deliquoring, no centrifuge. Displacement efficiency is a measured property of the
    cake, and a rule of thumb like "three displacements" presented as an answer is exactly the kind
    of fabrication that gets a wash under-sized on a real plant.

    Args:
        filtrate_volume_m3: Filtrate volume, m3.
        filter_area_m2: Filtration area, m2.
        pressure_drop_pa: Pressure drop, Pa.
        filtrate_viscosity_pa_s: Filtrate viscosity, Pa*s.
        specific_cake_resistance_m_per_kg: Measured alpha, m/kg.
        dry_cake_per_filtrate_kg_per_m3: Dry cake per unit filtrate, kg/m3.
        medium_resistance_per_m: Medium resistance R_m, 1/m.

    Returns:
        The total time, its cake and medium shares, the mean flux and the rate at the end.

    Raises:
        ValueError: If a volume, area, pressure, viscosity, resistance or loading is not positive,
            or the medium resistance is negative.
    """
    found = filtration.filtration_time(
        filtrate_volume_m3=filtrate_volume_m3,
        filter_area_m2=filter_area_m2,
        pressure_drop_pa=pressure_drop_pa,
        filtrate_viscosity_pa_s=filtrate_viscosity_pa_s,
        specific_cake_resistance_m_per_kg=specific_cake_resistance_m_per_kg,
        dry_cake_per_filtrate_kg_per_m3=dry_cake_per_filtrate_kg_per_m3,
        medium_resistance_per_m=medium_resistance_per_m,
    )
    return FiltrationTimeResult(
        total_time_seconds=found.total_time_seconds,
        total_time_minutes=found.total_time_minutes,
        total_time_hours=found.total_time_hours,
        cake_time_seconds=found.cake_time_seconds,
        medium_time_seconds=found.medium_time_seconds,
        cake_fraction_of_time=found.cake_fraction_of_time,
        average_flux_m3_per_m2_h=found.average_flux_m3_per_m2_h,
        final_rate_m3_per_s=found.final_rate_m3_per_s,
        cake_mass_kg=found.cake_mass_kg,
        basis=(
            "constant-pressure cake filtration, t = mu*alpha*c*V^2/(2*A^2*dP) + mu*R_m*V/(A*dP), "
            "from Darcy's law through a growing cake; INCOMPRESSIBLE cake, laminar flow, no cake "
            "cracking or shrinkage, and no wash, deliquoring or cake moisture"
        ),
    )


@server.tool()
def drying_time(
    dry_solid_mass_kg: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "Mass of BONE-DRY solid, in kg. Not the wet cake mass: a 100 kg cake at X = 0.25 "
                "holds 80 kg of dry solid and 20 kg of moisture."
            ),
        ),
    ],
    drying_area_m2: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "Area available for evaporation, in m2 — the exposed surface of the bed or tray, "
                "not the vessel's heat-transfer area. Agitation changes it."
            ),
        ),
    ],
    constant_rate_kg_per_m2_s: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "N_c, the constant-period drying rate, in kg of moisture per m2 per second, "
                "MEASURED from a drying curve on this material in this dryer. No default: it folds "
                "in the heat input, the gas velocity, the humidity and the geometry, so a rate "
                "borrowed from another dryer answers a different question."
            ),
        ),
    ],
    initial_moisture_dry_basis: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "X1, kg of moisture per kg of BONE-DRY solid at the start. Dry basis: a cake that "
                "is 20% wet on a wet basis is X = 0.25, not 0.20."
            ),
        ),
    ],
    critical_moisture_dry_basis: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "X_c, the moisture content at which the rate starts to fall, kg per kg dry solid, "
                "from the same drying curve."
            ),
        ),
    ],
    final_moisture_dry_basis: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "X2, the target, kg per kg dry solid. Must be above zero: this model's rate falls "
                "linearly to zero at zero moisture, so a bone-dry target takes infinite time. "
                "Where the equilibrium moisture is not zero, pass FREE moisture (X - X_e) "
                "throughout."
            ),
        ),
    ],
) -> DryingTimeResult:
    """Batch drying time through both periods, from a measured constant-rate drying curve.

    The classical two-period model: at a constant rate while the surface stays wet, then a
    falling rate below the critical moisture content, taken as falling linearly to zero moisture.
    The falling-rate leg is usually the longer of the two and is where a cycle overruns.

    **It is not a dryer model.** `N_c` is measured on this material in this dryer and carries the
    heat input, gas velocity, humidity and geometry inside it. Nothing here derives one from a
    jacket temperature or a vacuum level.

    **It assumes the equilibrium moisture is zero.** A solid that holds bound solvent never reaches
    zero however long it runs, and the logarithm would say infinity — so where `X_e` is not zero,
    every `X` here must be *free* moisture, `X - X_e`.

    **A drying time is not a specification.** Loss-on-drying, residual solvent by GC, and a
    polymorph that desolvates or converts as it dries are analytical and solid-state questions this
    arithmetic cannot see. A cycle that satisfies this model can still come off wet or off-form.
    Drying an energetic or thermally unstable solid is a **thermal-safety** question first, and no
    drying time from here says anything about whether the temperature is safe to hold.

    Args:
        dry_solid_mass_kg: Bone-dry solid mass, kg.
        drying_area_m2: Evaporation area, m2.
        constant_rate_kg_per_m2_s: Measured constant-period rate N_c, kg/(m2*s).
        initial_moisture_dry_basis: X1, kg/kg dry solid.
        critical_moisture_dry_basis: X_c, kg/kg dry solid.
        final_moisture_dry_basis: X2, kg/kg dry solid, above zero.

    Returns:
        The two periods, the total time and the moisture removed.

    Raises:
        ValueError: If a mass, area or rate is not positive, if the final moisture is at or below
            zero, if the target is not below the start, or if the critical moisture is below the
            target.
    """
    found = drying.drying_time(
        dry_solid_mass_kg=dry_solid_mass_kg,
        drying_area_m2=drying_area_m2,
        constant_rate_kg_per_m2_s=constant_rate_kg_per_m2_s,
        initial_moisture_dry_basis=initial_moisture_dry_basis,
        critical_moisture_dry_basis=critical_moisture_dry_basis,
        final_moisture_dry_basis=final_moisture_dry_basis,
    )
    return DryingTimeResult(
        constant_rate_seconds=found.constant_rate_seconds,
        falling_rate_seconds=found.falling_rate_seconds,
        total_time_seconds=found.total_time_seconds,
        total_time_hours=found.total_time_hours,
        moisture_removed_kg=found.moisture_removed_kg,
        starts_in_the_falling_rate_period=found.starts_in_the_falling_rate_period,
        basis=(
            "two-period batch drying on a measured constant rate: t1 = m_s*(X1 - X_c)/(A*N_c) then "
            "t2 = m_s*X_c/(A*N_c)*ln(X_c/X2), with the falling rate taken as LINEAR to zero "
            "moisture and the equilibrium moisture taken as zero; not a dryer model and not a "
            "residual-solvent specification"
        ),
    )
