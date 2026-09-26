"""The `kinetics` MCP tool surface: rate and ideal-reactor arithmetic from supplied parameters.

**These docstrings are the prompt**, and the thing they must get across here is what this server
*cannot* do, because its name promises more than it delivers and the gap is exactly where a number
would be invented.

**Nothing here fits anything.** Every tool takes a rate constant, an activation energy and an order
that a chemist already has — from their own kinetic study, from a literature value, or from a
supervisor's estimate. There is no regression, no time-course data, no fitting and no goodness of
fit. A question of the form "here is my concentration-against-time data, what is the rate law" is
one this server refuses to answer, and the refusal is the honest response rather than a limitation
to work around: fitting is a different operation with a different dependency closure, and it is
deliberately not built (see `README.md`).

**Every reactor here is ideal and isothermal.** Perfect mixing, no dispersion, no mass-transfer
limitation, constant temperature. Real reactors depart from all four, and the departures are what
scale-up problems are usually made of — so a conversion from this server is what the *kinetics*
predict, not what the vessel will do.

**The boundary against `servers/thermalsafety` is deliberate and narrow.** That server already
extrapolates along Arrhenius — of `q`, the specific heat-release rate of a *decomposition*, inside
a TMR_ad inversion, returning a temperature. This server extrapolates `k`, the rate constant of the
reaction being run, and returns a rate constant. Nothing here returns a TMR, a T_D24, a criticality
class or an adiabatic rise; ask that server. The two activation energies are different numbers
about different processes, and keeping the tools apart is what stops one being quoted as the other.

Every answer carries `basis`: the equation it came out of and the assumption that equation makes.

The five closed-form tools are synchronous and microseconds. The integrator is the one that does
real work: its step count is derived from the caller's rate constant up to `MAX_INTEGRATION_STEPS`,
which is up to half a second of CPU, so it runs off the event loop behind an admission ceiling
(`engine/admission.py`) rather than on the loop that answers `/healthz`.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp_server_kit.limits import env_bound
from pydantic import BaseModel, Field

from chemclaw_mcp_kinetics.engine import arrhenius, reactors
from chemclaw_mcp_kinetics.engine.admission import (
    DEFAULT_MAX_CONCURRENT_INTEGRATIONS,
    Admission,
)

server = FastMCP("kinetics")

# Built at import; a test that needs a different ceiling replaces this attribute.
_admission = Admission(
    env_bound(
        "CHEMCLAW_KINETICS_MAX_CONCURRENT_INTEGRATIONS",
        default=DEFAULT_MAX_CONCURRENT_INTEGRATIONS,
        minimum=1,
        consequence="this pod would refuse every semi-batch integration it is asked for",
    )
)


class RateConstantResult(BaseModel):
    """A rate constant carried to another temperature."""

    rate_constant: float = Field(
        description=(
            "k at the target temperature, in the SAME unit as the reference rate constant given. "
            "This server never names that unit, because it is a consequence of the reaction order."
        )
    )
    rate_ratio: float = Field(
        description="k(target)/k(reference) — unitless, and usually the number actually wanted."
    )
    extrapolated_by_k: float = Field(
        description=(
            "How far the answer was carried, in kelvin. Signed: positive is an extrapolation up. "
            "Heating and cooling are not equally safe, since a mechanism that changes usually does "
            "so on heating."
        )
    )
    far_from_the_measurement: bool = Field(
        description=(
            "True when the gap exceeds 50 K, where a second measured point is the honest answer. "
            "Reported rather than refused: how far is too far depends on whether the mechanism "
            "changes, which no arithmetic can know."
        )
    )
    basis: str


class ActivationEnergyResult(BaseModel):
    """An activation energy determined by two measured points."""

    activation_energy_kj_per_mol: float
    ln_pre_exponential: float = Field(
        description=(
            "ln A, in the same unit as the rate constants. A logarithm because A itself routinely "
            "overflows a float for a real reaction, and because the literature comparison is the "
            "logarithm anyway."
        )
    )
    span_k: float = Field(description="The temperature gap the two points cover, in kelvin.")
    span_is_narrow: bool = Field(
        description=(
            "True below a 10 K span, where ordinary measurement error in the two rate constants "
            "dominates the answer rather than the temperature dependence does."
        )
    )
    basis: str


class BatchResult(BaseModel):
    """Conversion in an isothermal batch reactor."""

    conversion: float = Field(description="Fractional conversion, between 0 and 1 (0.9 = 90%).")
    basis: str


class BatchTimeResult(BaseModel):
    """The time an isothermal batch reactor needs."""

    time_seconds: float
    time_hours: float
    basis: str


class ContinuousResult(BaseModel):
    """The same residence time in both ideal continuous reactors, side by side."""

    plug_flow_conversion: float = Field(description="Fractional conversion in an ideal PFR.")
    stirred_tank_conversion: float = Field(description="Fractional conversion in an ideal CSTR.")
    plug_flow_advantage: float = Field(
        description=(
            "PFR conversion divided by CSTR conversion at the same residence time — always at "
            "least 1. A CSTR sits entirely at its outlet composition, so the whole reactor runs at "
            "the slowest rate in the system."
        )
    )
    basis: str


class ProfilePoint(BaseModel):
    """One instant of a semi-batch dose."""

    time_seconds: float
    dosed_fraction: float = Field(description="How much of the dose has gone in, 0 to 1.")
    accumulated_fraction: float = Field(
        description=(
            "UNREACTED dosed reagent present, as a fraction of the whole dose. This is the number "
            "the question is about: the material a cooling failure would have to absorb at once."
        )
    )


class AccumulationResult(BaseModel):
    """A semi-batch dose, its accumulation profile and the worst instant in it."""

    peak_accumulation_fraction: float = Field(
        description="The highest unreacted fraction reached at any point in the dose."
    )
    peak_at_seconds: float
    accumulation_at_end_of_dose: float
    profile: list[ProfilePoint]
    basis: str


@server.tool()
def rate_constant_at_temperature(
    target_temperature_c: Annotated[float, Field(description="The temperature wanted, in °C.")],
    reference_temperature_c: Annotated[
        float, Field(description="The temperature the rate constant was measured at, in °C.")
    ],
    reference_rate_constant: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "The measured rate constant, in whatever unit its reaction order implies. The unit "
                "is not asked for and not converted; the answer comes back in the same one."
            ),
        ),
    ],
    activation_energy_kj_per_mol: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "The activation energy of THIS reaction, in kJ/mol. There is no default: an "
                "assumed E_a is the whole of the answer's error, so supplying one would be this "
                "tool inventing the number the chemist came to ask about. A typical organic "
                "reaction is 50-100 kJ/mol, and 'the rate doubles every 10 °C' near room "
                "temperature is about 53."
            ),
        ),
    ],
) -> RateConstantResult:
    """Carry a measured rate constant to another temperature along Arrhenius.

    `k(T) = k(T_ref)·exp(-E_a/R·(1/T - 1/T_ref))`. Exact arithmetic on the inputs, so **the answer's
    error is entirely the error in the activation energy supplied** — which is why the tool will not
    guess one and why the distance extrapolated comes back with the answer.

    **This is not a TMR, a T_D24 or a thermal-safety figure.** It is the rate constant of the
    reaction being run. A question about what happens if cooling fails is `thermalsafety`'s, and
    its Arrhenius extrapolation is of a *decomposition's* heat-release rate, which is a different
    number about a different process.

    Args:
        target_temperature_c: The temperature wanted, in °C.
        reference_temperature_c: Where the rate constant was measured, in °C.
        reference_rate_constant: The measured rate constant.
        activation_energy_kj_per_mol: This reaction's activation energy, in kJ/mol.

    Returns:
        The rate constant there, the ratio to the measured one, and how far it was carried.

    Raises:
        ValueError: If a temperature is below absolute zero, or a value is not positive.
    """
    result = arrhenius.rate_constant_at(
        target_temperature_c,
        reference_temperature_c=reference_temperature_c,
        reference_rate_constant=reference_rate_constant,
        activation_energy_kj_per_mol=activation_energy_kj_per_mol,
    )
    return RateConstantResult(
        rate_constant=result.rate_constant,
        rate_ratio=result.rate_ratio,
        extrapolated_by_k=result.extrapolated_by_k,
        far_from_the_measurement=result.far_from_the_measurement,
        basis=(
            "Arrhenius, k(T) = k(T_ref)·exp(-E_a/R·(1/T - 1/T_ref)), from one measured point and a "
            "supplied activation energy. Assumes one mechanism over the whole range; the error is "
            "the error in E_a."
        ),
    )


@server.tool()
def activation_energy_from_two_rates(
    lower_temperature_c: Annotated[float, Field(description="The cooler temperature, in °C.")],
    lower_rate_constant: Annotated[
        float, Field(gt=0, description="The rate constant measured there.")
    ],
    upper_temperature_c: Annotated[float, Field(description="The warmer temperature, in °C.")],
    upper_rate_constant: Annotated[
        float, Field(gt=0, description="The rate constant measured there, in the same unit.")
    ],
) -> ActivationEnergyResult:
    """Activation energy and ln A from two measured (temperature, rate constant) pairs.

    **This is exact algebra, not a fit.** Two points determine a line through two points, so there
    is no residual, no r² and no confidence interval — nothing is left over. A regression over
    three or more points is a different operation and this server does not do it; asking for one
    should get an honest "not here" rather than this tool run twice.

    A rate constant that *falls* with temperature is refused rather than turned into a negative
    activation energy. That happens for real reasons — a pre-equilibrium, a changing mechanism, a
    catalyst decomposing, or the two points measuring different things — and every one of them is
    something a chemist needs told rather than folded into a sign.

    Args:
        lower_temperature_c: The cooler temperature, in °C.
        lower_rate_constant: The rate constant measured there.
        upper_temperature_c: The warmer temperature, in °C.
        upper_rate_constant: The rate constant measured there, same unit.

    Returns:
        The activation energy, ln A, the span covered and whether that span is too narrow to trust.

    Raises:
        ValueError: If the temperatures are equal or in the wrong order, a rate constant is not
            positive, or the rate falls with temperature.
    """
    pair = arrhenius.activation_energy_from_two_points(
        lower_temperature_c=lower_temperature_c,
        lower_rate_constant=lower_rate_constant,
        upper_temperature_c=upper_temperature_c,
        upper_rate_constant=upper_rate_constant,
    )
    return ActivationEnergyResult(
        activation_energy_kj_per_mol=pair.activation_energy_kj_per_mol,
        ln_pre_exponential=pair.ln_pre_exponential,
        span_k=pair.span_k,
        span_is_narrow=pair.span_is_narrow,
        basis=(
            "E_a = R·ln(k2/k1)/(1/T1 - 1/T2), exact through two points. Not a regression: no "
            "residual and no goodness of fit exist, because nothing is left over."
        ),
    )


@server.tool()
def batch_conversion_after(
    rate_constant: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "k, in the unit the order implies — s⁻¹ for first order, (concentration·s)⁻¹ for "
                "second. Not converted: the unit follows from `order` and your concentration unit."
            ),
        ),
    ],
    initial_concentration: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "C₀ of the limiting reagent, in your own unit. Required rather than defaulted, "
                "because for every order but the first the absolute value changes the answer."
            ),
        ),
    ],
    time_seconds: Annotated[float, Field(ge=0, description="How long the reaction runs, seconds.")],
    order: Annotated[
        float, Field(default=1.0, ge=0, description="Order in the limiting reagent.")
    ] = 1.0,
) -> BatchResult:
    """Fractional conversion in an ideal isothermal batch reactor after a given time.

    Integrates `-dC/dt = k·Cⁿ` in closed form. **Ideal and isothermal**: perfectly mixed, constant
    volume, constant temperature, no mass-transfer limitation and no reverse reaction. That is what
    the kinetics predict, not what a vessel will do — the departures are usually what a scale-up
    problem is made of.

    Args:
        rate_constant: k, in the unit its order implies.
        initial_concentration: C₀ of the limiting reagent.
        time_seconds: Reaction time in seconds.
        order: Order in the limiting reagent; 1.0 is much the most common.

    Returns:
        Fractional conversion between 0 and 1.

    Raises:
        ValueError: If a value is not positive or the order is negative.
    """
    return BatchResult(
        conversion=reactors.batch_conversion(
            rate_constant=rate_constant,
            initial_concentration=initial_concentration,
            time_seconds=time_seconds,
            order=order,
        ),
        basis=(
            f"integrated -dC/dt = k·C^{order:g} for an ideal isothermal batch reactor; perfect "
            "mixing, constant volume, no reverse reaction and no mass-transfer limitation"
        ),
    )


@server.tool()
def batch_time_to_reach(
    rate_constant: Annotated[float, Field(gt=0, description="k, in the unit the order implies.")],
    initial_concentration: Annotated[
        float, Field(gt=0, description="C₀ of the limiting reagent, in your own unit.")
    ],
    conversion: Annotated[
        float,
        Field(
            gt=0,
            lt=1,
            description=(
                "The fractional conversion wanted — 0.9 for 90%, NOT 90. Values above 0.999 are "
                "refused: there the time is set by mixing, impurities and the reverse reaction "
                "rather than by the rate law."
            ),
        ),
    ],
    order: Annotated[
        float, Field(default=1.0, ge=0, description="Order in the limiting reagent.")
    ] = 1.0,
) -> BatchTimeResult:
    """How long an ideal isothermal batch reactor needs to reach a given conversion.

    The exact inverse of `batch_conversion_after`, in closed form rather than by search — so the
    two cannot disagree at the fourth decimal the way a solver and its forward model can.

    Same idealisations, and the same warning: this is the time the kinetics imply, not a cycle time.

    Args:
        rate_constant: k, in the unit its order implies.
        initial_concentration: C₀ of the limiting reagent.
        conversion: The fraction wanted, between 0 and 1.
        order: Order in the limiting reagent.

    Returns:
        The time in seconds and in hours.

    Raises:
        ValueError: If a value is not positive, the conversion is outside the meaningful range, or
            the order is negative.
    """
    seconds = reactors.time_for_batch_conversion(
        rate_constant=rate_constant,
        initial_concentration=initial_concentration,
        conversion=conversion,
        order=order,
    )
    return BatchTimeResult(
        time_seconds=seconds,
        time_hours=seconds / 3600.0,
        basis=(
            f"closed-form inverse of the integrated -dC/dt = k·C^{order:g} rate law for an ideal "
            "isothermal batch reactor"
        ),
    )


@server.tool()
def continuous_reactor_conversion(
    rate_constant: Annotated[float, Field(gt=0, description="k, in the unit the order implies.")],
    inlet_concentration: Annotated[
        float, Field(gt=0, description="C₀ in the feed, in your own unit.")
    ],
    residence_time_seconds: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "τ = V/Q, the reactor volume over the volumetric flow rate, in seconds. For a "
                "90-second residence time, pass 90."
            ),
        ),
    ],
    order: Annotated[
        float, Field(default=1.0, ge=0, description="Order in the limiting reagent.")
    ] = 1.0,
) -> ContinuousResult:
    """Conversion in an ideal PFR and an ideal CSTR at the same residence time, side by side.

    Both are returned together because the comparison is the answer a chemist moving a batch
    process to continuous actually needs, and because the size of the gap surprises people: at 90%
    conversion in first order an ideal CSTR needs **3.9 times** the residence time of an ideal PFR.

    The reason is structural rather than a coefficient. A CSTR is perfectly mixed, so the entire
    reactor sits at its *outlet* composition — the lowest concentration in the system, and therefore
    the slowest rate. A PFR has no back-mixing, so each element of fluid sees the full
    concentration profile, exactly as a batch reactor does over time. **A PFR is a batch reactor
    moving down a pipe**, and this tool computes it that way.

    Ideal in both cases: no dispersion, no channelling, no residence-time distribution, isothermal.
    A real tubular reactor at low Reynolds number is a long way from plug flow.

    Args:
        rate_constant: k, in the unit its order implies.
        inlet_concentration: C₀ in the feed.
        residence_time_seconds: τ = V/Q, in seconds.
        order: Order in the limiting reagent.

    Returns:
        Both conversions and the ratio between them.

    Raises:
        ValueError: If a value is not positive or the order is negative.
    """
    common = {
        "rate_constant": rate_constant,
        "initial_concentration": inlet_concentration,
        "residence_time_seconds": residence_time_seconds,
        "order": order,
    }
    plug = reactors.pfr_conversion(**common)
    tank = reactors.cstr_conversion(**common)
    return ContinuousResult(
        plug_flow_conversion=plug,
        stirred_tank_conversion=tank,
        # A zero-conversion CSTR only happens at zero residence time, which is refused above; the
        # guard is here so a future change to that bound cannot turn this into a ZeroDivisionError,
        # which would reach the model as an opaque error id rather than as a number.
        plug_flow_advantage=plug / tank if tank > 0.0 else 1.0,
        basis=(
            f"ideal isothermal reactors at order {order:g}: PFR by the integrated batch rate law "
            "with τ as time, CSTR by the steady-state balance C₀ - C = τ·k·Cⁿ at outlet "
            "composition. No dispersion, no residence-time distribution."
        ),
    )


@server.tool()
async def semibatch_accumulation_profile(
    rate_constant: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "k for the reaction between the dosed reagent and the co-reagent, in the unit the "
                "two orders imply."
            ),
        ),
    ],
    dose_time_seconds: Annotated[
        float, Field(gt=0, description="How long the addition takes, at a constant rate, seconds.")
    ],
    initial_volume: Annotated[
        float, Field(gt=0, description="Volume in the vessel before dosing, in your own unit.")
    ],
    dosed_moles: Annotated[
        float, Field(gt=0, description="Total moles of the dosed reagent over the whole addition.")
    ],
    initial_coreagent_concentration: Annotated[
        float, Field(gt=0, description="The co-reagent's concentration in the vessel at the start.")
    ],
    dosed_volume: Annotated[
        float,
        Field(
            default=0.0,
            ge=0,
            description=(
                "The volume the dose occupies, same unit as `initial_volume`. Pass 0 for a neat "
                "addition small enough to ignore."
            ),
        ),
    ] = 0.0,
    order_in_dosed: Annotated[
        float, Field(default=1.0, ge=0, description="Order in the dosed reagent.")
    ] = 1.0,
    order_in_coreagent: Annotated[
        float,
        Field(
            default=1.0,
            ge=0,
            description=(
                "Order in the co-reagent. Pass 0 for a large excess, which makes the reaction "
                "pseudo-first-order in the dosed reagent."
            ),
        ),
    ] = 1.0,
) -> AccumulationResult:
    """How much unreacted dosed reagent builds up during a constant-rate semi-batch addition.

    **Accumulation is why "add it slowly" is a safety decision rather than a preference.** The
    unreacted dosed reagent present at any instant is the material a cooling failure would have to
    absorb all at once, so the peak of this profile is the case a dose time is chosen against.

    Integrated numerically — the volume and both concentrations move together, so there is no
    closed form.

    **Isothermal**, which is the assumption to check before using the number. The vessel is taken as
    held at temperature, which is what a jacket is for. If the jacket cannot hold it, the batch
    warms, `k` rises and accumulation falls — while the danger rises. That coupled problem is
    `servers/thermalsafety`'s, from calorimetry, and **this tool's peak accumulation is an input to
    that question, not an answer to it**: it says how much is there, not what the temperature would
    reach.

    Args:
        rate_constant: k for the dosed/co-reagent reaction.
        dose_time_seconds: Addition time at constant rate, seconds.
        initial_volume: Vessel volume before dosing.
        dosed_moles: Total moles added over the addition.
        initial_coreagent_concentration: Co-reagent concentration at the start.
        dosed_volume: Volume the dose occupies; 0 to ignore.
        order_in_dosed: Order in the dosed reagent.
        order_in_coreagent: Order in the co-reagent; 0 for a large excess.

    Returns:
        The peak accumulation and when it occurs, the value at the end of the dose, and a sampled
        profile.

    Raises:
        ValueError: If a value is not positive or an order is negative, if the dose is too fast
            or runs a reagent out in a way the integrator cannot follow, or if the pod is already
            running as many integrations as it admits.
    """
    # Off the event loop and behind the ceiling: the step count is derived from `rate_constant`,
    # so the cost is the caller's to set. `asyncio.shield` releases the slot when the work ends
    # rather than when the caller stops waiting, because cancelling the await does not stop the
    # worker thread.
    _admission.acquire("semibatch_accumulation_profile")
    profile = await _admission.hold(
        asyncio.to_thread(
            reactors.semibatch_accumulation,
            rate_constant=rate_constant,
            dose_time_seconds=dose_time_seconds,
            initial_volume=initial_volume,
            dosed_moles=dosed_moles,
            dosed_volume=dosed_volume,
            initial_coreagent_concentration=initial_coreagent_concentration,
            order_in_dosed=order_in_dosed,
            order_in_coreagent=order_in_coreagent,
        ),
        1,
    )
    return AccumulationResult(
        peak_accumulation_fraction=profile.peak_accumulation_fraction,
        peak_at_seconds=profile.peak_at_seconds,
        accumulation_at_end_of_dose=profile.accumulation_at_end_of_dose,
        profile=[
            ProfilePoint(
                time_seconds=point.time_seconds,
                dosed_fraction=point.dosed_fraction,
                accumulated_fraction=point.accumulated_fraction,
            )
            for point in profile.points
        ],
        basis=(
            "fixed-step RK4 over moles of both species with the volume rising linearly; ideal "
            "isothermal semi-batch, perfect mixing, constant dose rate, no energy balance"
        ),
    )
