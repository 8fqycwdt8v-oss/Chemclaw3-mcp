"""The `thermalsafety` MCP tool surface: runaway arithmetic from calorimetry numbers.

**These docstrings are the prompt**, and on this server that matters more than on most of the
fleet: every tool here returns a number a chemist may use to decide whether a batch is safe to
scale, and every one of them is an *arithmetic consequence of inputs the caller supplied*. So each
docstring states its units in every argument name, and each says what the tool is not evidence of —
because the failure mode is not a wrong formula, it is a right formula fed a heat capacity in the
wrong unit or an accumulation fraction somebody assumed.

**Nothing here measures anything.** There is no calorimetry model, no kinetics fit and no corpus:
every input is a number from a DSC, ARC or RC1 report that a person ran. A tool that cannot be
given one refuses rather than defaulting it — `mtsr` will not assume an accumulation fraction and
`tmr_ad` will not assume an activation energy — because a default here is the safety argument being
invented by the calculator instead of made by the chemist.

Every answer carries `basis`: the model it came out of and the assumption that model makes. A
number from a Semenov balance and a number from an adiabatic balance answer different questions,
and a result that does not say which it is cannot be put in a report.

The tools are synchronous. Measured rather than assumed, and the measurement corrected the guess
that preceded it: the slowest is `tmr_ad` at **63.7 µs**, because it solves for T_D24 by a
fixed 200-step bisection — not `semenov_critical_ambient` (16.3 µs), whose bisection stops on a
tolerance. `oxygen_balance_screen` is 4.5 µs and the two closed-form tools are under half a
microsecond. Four orders of magnitude below the point at which holding the event loop matters, with
no subprocess and no thread to pin. `servers/calc`'s admission ceiling exists because one call
there is minutes of
CPU across forked workers; nothing here is, so a ceiling would be a control with nothing to control.
A tool added here that grows real work must revisit both sentences.
"""

from __future__ import annotations

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from chemclaw_mcp_thermalsafety.engine import oxygen_balance as ob
from chemclaw_mcp_thermalsafety.engine import runaway, semenov

server = FastMCP("thermalsafety")

#: The longest molecular formula `oxygen_balance_screen` will parse. A bound on the input so the
#: cost cannot run away unpriced — the rule `docs/adding-a-server.md` states for a slow tool, which
#: applies to a fast one whose input length is unbounded. A real molecular formula is under 60
#: characters; 512 leaves room for a polymer repeat unit written out and refuses a payload.
MAX_FORMULA_CHARACTERS = 512


class AdiabaticRise(BaseModel):
    """The adiabatic temperature rise, with the balance it came out of."""

    adiabatic_temperature_rise_k: float = Field(
        description="ΔT_ad in kelvin — a temperature difference, so numerically identical in °C."
    )
    basis: str


class Mtsr(BaseModel):
    """The Maximum Temperature of the Synthesis Reaction."""

    mtsr_c: float = Field(description="MTSR in °C.")
    basis: str


class TimeToMaximumRate(BaseModel):
    """TMR_ad at one temperature, plus the temperature at which it equals a target."""

    tmr_ad_hours: float = Field(
        description="Time to maximum rate under adiabatic conditions, hours."
    )
    temperature_for_target_c: float | None = Field(
        description=(
            "The temperature in °C at which TMR_ad equals `target_hours` — T_D24 when that is 24. "
            "Null when no temperature between -99 and 500 °C reaches it, which `note` explains."
        )
    )
    note: str
    basis: str


class Criticality(BaseModel):
    """A Stoessel criticality class and the temperature ordering behind it."""

    criticality_class: int = Field(description="1 to 5; lower is a less critical ordering.")
    ordering: list[str] = Field(
        description=(
            "The four characteristic temperatures in ascending order, as `T_p = 20 °C` strings — "
            "the evidence for the class, so a reader can check it rather than trust it."
        )
    )
    interpretation: str
    basis: str


class HeatRemoval(BaseModel):
    """What the jacket can take out, total and per kilogram."""

    heat_removal_w: float = Field(
        description="U·A·(T_r - T_j) in W. Negative means the jacket is heating, not cooling."
    )
    specific_heat_removal_w_per_kg: float = Field(
        description="The same duty divided by the reaction mass, W/kg — comparable against a "
        "specific heat release rate from calorimetry."
    )
    basis: str


class SemenovResult(BaseModel):
    """The Semenov crossover, and the UN-rounded value beside the unrounded one."""

    critical_ambient_c: float = Field(
        description="The ambient temperature in °C above which this package self-heats without "
        "bound under the Semenov model."
    )
    critical_contents_c: float = Field(
        description="The contents' temperature in °C at the tangency."
    )
    self_heating_at_criticality_k: float = Field(
        description="R·T²/Ea in kelvin: the steady-state excess the contents run at just before "
        "the balance is lost."
    )
    heat_generation_at_criticality_w: float = Field(
        description="The decomposition's heat output in W at the tangency, which the loss term "
        "exactly matches there."
    )
    rounded_up_to_five_c: int = Field(
        description="The critical ambient rounded up to the next whole 5 °C, the UN Manual of "
        "Tests and Criteria §28.1.4.2 reporting convention."
    )
    basis: str


class OxygenBalanceResult(BaseModel):
    """The oxygen-balance screen: the number, the parse, and what it licenses."""

    oxygen_balance_percent: float = Field(
        description="Oxygen balance to CO2, percent by mass. Negative is oxygen-deficient."
    )
    molar_mass_g_per_mol: float = Field(
        description="Molar mass from the same element counts — check it against the value you "
        "already know; a mismatch means the formula was parsed differently than you wrote it."
    )
    composition: dict[str, float] = Field(description="The parsed element counts.")
    band: str
    interpretation: str
    basis: str


@server.tool()
def adiabatic_temperature_rise(
    heat_of_reaction_kj_per_mol: Annotated[
        float,
        Field(
            description="Reaction enthalpy in kJ per mole of the limiting reagent, from RC1 or a "
            "comparable reaction calorimeter. Either sign is accepted and the magnitude used. Not "
            "a computed gas-phase enthalpy, which says nothing about the solvated, mixed system a "
            "jacket has to remove heat from."
        ),
    ],
    moles: Annotated[float, Field(description="Moles of the limiting reagent in the batch, mol.")],
    mass_kg: Annotated[
        float,
        Field(
            description="Total mass of the reaction mass in kg — everything the heat is "
            "distributed into, solvent included, not the reagent's mass."
        ),
    ],
    specific_heat_kj_per_kg_k: Annotated[
        float,
        Field(
            description="Specific heat capacity of the reaction mass in kJ/(kg·K). About 1.8-2.1 "
            "for common organic solvents, 4.18 for water. The mixture's value, not the solute's."
        ),
    ],
) -> AdiabaticRise:
    """The temperature the batch would reach if every joule of reaction heat stayed in it.

    ΔT_ad = |ΔH_r| · n / (m · c_p). The first number in every runaway assessment: it is what MTSR,
    the Stoessel class and the whole cooling-failure scenario are built on.

    **This is not a prediction that the batch reaches this temperature.** It is the bound assuming
    perfect insulation and complete conversion at the worst instant — no heat loss, no boiling, no
    reflux. A real vessel loses heat and a real solvent boils, which is exactly what `mtsr` and
    `stoessel_criticality_class` go on to account for. Nor is it a decomposition rise: this is the
    *synthesis* reaction's heat. A secondary decomposition has its own ΔT_ad from DSC, and the two
    are added nowhere in this server because whether they add depends on the scenario.
    """
    rise = runaway.adiabatic_temperature_rise(
        heat_of_reaction_kj_per_mol=heat_of_reaction_kj_per_mol,
        moles=moles,
        mass_kg=mass_kg,
        specific_heat_kj_per_kg_k=specific_heat_kj_per_kg_k,
    )
    return AdiabaticRise(
        adiabatic_temperature_rise_k=rise,
        basis=(
            "Adiabatic balance |ΔH|·n/(m·cp): no heat loss, no boiling, complete conversion of the "
            "moles given. The synthesis reaction only; a secondary decomposition has its own."
        ),
    )


@server.tool()
def mtsr(
    process_temperature_c: Annotated[
        float, Field(description="The intended process temperature in °C.")
    ],
    adiabatic_temperature_rise_k: Annotated[
        float,
        Field(
            description="ΔT_ad for the synthesis reaction in K, from `adiabatic_temperature_rise` "
            "or from the calorimetry directly."
        ),
    ],
    accumulation_fraction: Annotated[
        float,
        Field(
            description="The unreacted fraction of the limiting reagent at the worst moment, 0-1. "
            "Read off the calorimetric conversion curve. There is deliberately no default: this "
            "number IS the safety argument, and a process is made safe by lowering it."
        ),
    ],
) -> Mtsr:
    """Maximum Temperature of the Synthesis Reaction — where a cooling failure takes the batch.

    MTSR = T_p + X_ac · ΔT_ad. The single temperature a runaway assessment turns on: compared
    against the boiling point and against T_D24, it decides the Stoessel criticality class.

    **The accumulation fraction is an input and will not be guessed.** Assuming 1.0 makes every
    dosed process look critical; assuming anything lower invents the argument the chemist is
    supposed to be making from their own conversion curve. If you do not have it, the answer is to
    measure it, not to pick one.

    **This is not a maximum attainable temperature.** It is where the *synthesis* reaction stops. A
    secondary decomposition triggered on the way there goes further, which is what T_D24 and
    `stoessel_criticality_class` are for.
    """
    value = runaway.mtsr(
        process_temperature_c=process_temperature_c,
        adiabatic_temperature_rise_k=adiabatic_temperature_rise_k,
        accumulation_fraction=accumulation_fraction,
    )
    return Mtsr(
        mtsr_c=value,
        basis=(
            f"T_p + X_ac·ΔT_ad at an accumulation fraction of {accumulation_fraction}, which the "
            "caller supplied. The synthesis reaction's end point, not a maximum attainable "
            "temperature."
        ),
    )


@server.tool()
def tmr_ad(
    temperature_c: Annotated[
        float,
        Field(
            description="The temperature in °C to evaluate TMR_ad at — usually MTSR, since the "
            "question is how long there is once a cooling failure has put the batch there."
        ),
    ],
    heat_release_rate_w_per_kg: Annotated[
        float,
        Field(
            description="Specific heat release rate of the decomposition in W/kg, at "
            "`reference_temperature_c`. From DSC or ARC. Watts per kilogram, not kilojoules."
        ),
    ],
    activation_energy_kj_per_mol: Annotated[
        float,
        Field(
            description="Apparent activation energy of the decomposition in kJ/mol, from the same "
            "calorimetry. Typically 50-200; there is no default because the extrapolation below is "
            "exponential in it."
        ),
    ],
    specific_heat_kj_per_kg_k: Annotated[
        float, Field(description="Specific heat capacity of the reaction mass, kJ/(kg·K).")
    ],
    reference_temperature_c: Annotated[
        float | None,
        Field(
            description="The temperature in °C at which `heat_release_rate_w_per_kg` was measured. "
            "Omit it when the rate was read at the temperature asked about. **0 means 0 °C** — an "
            "ice-bath isothermal is an ordinary reference for a peroxide or a diazo compound, and "
            "it is not the same statement as omitting this."
        ),
    ] = None,
    target_hours: Annotated[
        float,
        Field(
            description="Also solve for the temperature at which TMR_ad equals this many hours. "
            "24 gives T_D24, the input `stoessel_criticality_class` wants."
        ),
    ] = 24.0,
) -> TimeToMaximumRate:
    """Time to Maximum Rate under adiabatic conditions — how long there is before the runaway.

    TMR_ad = c_p·R·T² / (q·E_a), the Townsend-Tou expression, plus the inverse: the temperature at
    which TMR_ad equals `target_hours`. At 24 h that inverse is T_D24, which with MTSR and the
    boiling point is what `stoessel_criticality_class` classifies.

    **The inverse is an Arrhenius extrapolation and its error grows with distance.** A rate measured
    at 200 °C carried down to 60 °C passes the activation energy's uncertainty through an
    exponential; an isothermal test near the temperature of interest is what settles a T_D24, and
    this returns an estimate for deciding which test to book.

    **It assumes zero order and no reactant consumption**, which is conservative for a simple
    decomposition and **optimistic for an autocatalytic one** — an induction period this model
    cannot see is exactly how an autocatalytic decomposition surprises people. If the calorimetry
    shows autocatalysis, this number is not the one to plan on.

    **Adiabatic means no cooling at all.** For a package that does lose heat to its surroundings,
    the question is a Semenov one and `semenov_critical_ambient` is the tool.
    """
    # **`is None`, not truthiness, and truthiness discarded a stated 0 °C.** This field defaulted to
    # `0.0` and was read with `if reference_temperature_c else temperature_c`, so a caller who said
    # "the rate was measured at 0 °C" — an ice-bath isothermal, the ordinary reference for a
    # peroxide
    # or a diazo compound — had that replaced by `temperature_c`, skipping the Arrhenius
    # extrapolation entirely and using q(0 °C) as though it were q(T_asked).
    #
    # Driven at q = 1 W/kg, E_a = 100 kJ/mol, c_p = 1.8 kJ/(kg·K), asked at 150 °C: a stated
    # reference of **0.0 °C** gave TMR_ad = 7.44 h and T_D24 = +132 °C, while **0.001 °C** gave
    # 1.24e-06 h and -12.7 °C. A thousandth of a degree moved the answer by a factor of six
    # million, because q(150 °C) is 6.0e6 W/kg and the tool was using 1. Carried into
    # `stoessel_criticality_class`, which names this tool at `target_hours=24` as its source, that
    # is
    # **class 2** ("a cooling failure reaches neither barrier") against **class 5** ("the scenario
    # has
    # to be eliminated by process design") — the reassuring end of the scale instead of the one that
    # stops a process. The only trace was `basis` naming a temperature the caller had not written.
    reference = temperature_c if reference_temperature_c is None else reference_temperature_c
    hours = runaway.time_to_maximum_rate_hours(
        temperature_c=temperature_c,
        heat_release_rate_w_per_kg=_rate_at(
            temperature_c=temperature_c,
            reference_temperature_c=reference,
            heat_release_rate_w_per_kg=heat_release_rate_w_per_kg,
            activation_energy_kj_per_mol=activation_energy_kj_per_mol,
        ),
        activation_energy_kj_per_mol=activation_energy_kj_per_mol,
        specific_heat_kj_per_kg_k=specific_heat_kj_per_kg_k,
    )
    try:
        target_c: float | None = runaway.temperature_for_tmr(
            target_hours=target_hours,
            reference_temperature_c=reference,
            heat_release_rate_w_per_kg=heat_release_rate_w_per_kg,
            activation_energy_kj_per_mol=activation_energy_kj_per_mol,
            specific_heat_kj_per_kg_k=specific_heat_kj_per_kg_k,
        )
        note = f"TMR_ad reaches {target_hours} h at {target_c:.1f} °C."
    except runaway.ThermalInputError as refusal:
        target_c = None
        note = str(refusal)
    return TimeToMaximumRate(
        tmr_ad_hours=hours,
        temperature_for_target_c=target_c,
        note=note,
        basis=(
            f"Townsend-Tou TMR_ad, zero order, no reactant consumption, rate extrapolated along "
            f"Arrhenius from {reference} °C. Adiabatic: no heat loss of any kind. Optimistic for "
            "an autocatalytic decomposition, whose induction period this model cannot represent."
        ),
    )


def _rate_at(
    *,
    temperature_c: float,
    reference_temperature_c: float,
    heat_release_rate_w_per_kg: float,
    activation_energy_kj_per_mol: float,
) -> float:
    """Arrhenius-extrapolate a measured specific heat release rate to another temperature, W/kg.

    Exists because `tmr_ad` takes the rate at a reference point and the TMR at a *different*
    temperature, and doing that conversion inline in the tool body would put the only copy of the
    Arrhenius expression somewhere untested. It is one call to `semenov.heat_generation_w` over a
    1 kg basis, which is what makes it a wrapper rather than a second implementation.
    """
    return semenov.heat_generation_w(
        temperature_c=temperature_c,
        mass_kg=1.0,
        heat_release_rate_w_per_kg=heat_release_rate_w_per_kg,
        reference_temperature_c=reference_temperature_c,
        activation_energy_kj_per_mol=activation_energy_kj_per_mol,
    )


@server.tool()
def stoessel_criticality_class(
    process_temperature_c: Annotated[
        float, Field(description="The intended process temperature T_p, in °C.")
    ],
    mtsr_c: Annotated[
        float,
        Field(description="MTSR in °C, from `mtsr` or from the calorimetry directly."),
    ],
    max_technical_temperature_c: Annotated[
        float,
        Field(
            description="MTT in °C: the boiling point of the reaction mass at the operating "
            "pressure for an open system, or the vessel's maximum permissible temperature for a "
            "closed one. Which of the two it is changes what class 3 and 4 mean."
        ),
    ],
    decomposition_t_d24_c: Annotated[
        float,
        Field(
            description="T_D24 in °C: the temperature at which the secondary decomposition's "
            "TMR_ad is 24 hours. From `tmr_ad` with `target_hours=24`, or measured."
        ),
    ],
) -> Criticality:
    """Classify a cooling-failure scenario 1-5 by the order of its four characteristic temperatures.

    The standard Stoessel classification (*Thermal Safety of Chemical Processes*, Wiley 2008). The
    class follows entirely from the ordering of T_p, MTSR, MTT and T_D24 — so the ordering is
    returned beside the class, and a reader who disagrees with the class can see which comparison
    produced it.

    **A class is not a verdict and a low class is not a safe process.** Class 1 says a cooling
    failure has a bounded outcome under these four numbers; it says nothing about whether they are
    right, about the dosing regime, about a wrong charge, or about anything outside the
    cooling-failure scenario. Class 3 and 4 both rest on evaporative cooling being the barrier, and
    **this classification does not check that it is sufficient** — the condenser duty, the vent line
    and the scrubber at the vapour rate MTSR produces are a separate calculation this server does
    not do.
    """
    result = runaway.stoessel_class(
        process_temperature_c=process_temperature_c,
        mtsr_c=mtsr_c,
        max_technical_temperature_c=max_technical_temperature_c,
        decomposition_t_d24_c=decomposition_t_d24_c,
    )
    return Criticality(
        criticality_class=result.criticality_class,
        ordering=[f"{name} = {value:g} °C" for name, value in result.ordering],
        interpretation=result.interpretation,
        basis=(
            "Stoessel criticality from the ordering of T_p, MTSR, MTT and T_D24 alone. Classes 3 "
            "and 4 assume evaporative cooling is available; whether it is sufficient at this "
            "vapour rate is not checked here."
        ),
    )


@server.tool()
def heat_removal_capacity(
    heat_transfer_coefficient_w_per_m2_k: Annotated[
        float,
        Field(
            description="Overall heat transfer coefficient U in W/(m²·K), measured or estimated "
            "for this vessel and this reaction mass."
        ),
    ],
    heat_exchange_area_m2: Annotated[
        float,
        Field(description="Wetted heat-exchange area A in m² at the fill level in question."),
    ],
    reactor_temperature_c: Annotated[float, Field(description="Reaction mass temperature in °C.")],
    jacket_temperature_c: Annotated[
        float, Field(description="Jacket or service-fluid temperature in °C.")
    ],
    mass_kg: Annotated[float, Field(description="Reaction mass in kg.")],
) -> HeatRemoval:
    """What the jacket can take out right now, as a total duty and per kilogram.

    q_ex = U·A·(T_r - T_j), with q_ex/m so it can be compared directly against a specific heat
    release rate from calorimetry. That comparison is the point: a reaction releasing more W/kg than
    the jacket removes is accumulating heat, whatever the vessel's nominal rating says.

    **The sign is kept.** A jacket warmer than the reactor returns a negative number, because "the
    cooling is a heat source right now" is a real state — a batch being brought up to temperature is
    in it — and an absolute value would hide it.

    **This is a steady-state snapshot, not a dynamic model.** It says nothing about U falling as the
    batch thickens, about fouling, about the jacket's own thermal inertia, or about whether the
    service loop can sustain the duty. The classic scale-up failure is in none of those terms: A/V
    falls as volume rises, so a duty per kilogram that was ample in a 1 L flask is not in a 1 m³
    reactor — which is visible here only if you pass the large vessel's own A and m.
    """
    total, specific = runaway.heat_removal_capacity(
        heat_transfer_coefficient_w_per_m2_k=heat_transfer_coefficient_w_per_m2_k,
        heat_exchange_area_m2=heat_exchange_area_m2,
        reactor_temperature_c=reactor_temperature_c,
        jacket_temperature_c=jacket_temperature_c,
        mass_kg=mass_kg,
    )
    return HeatRemoval(
        heat_removal_w=total,
        specific_heat_removal_w_per_kg=specific,
        basis=(
            "U·A·ΔT at one instant, from the U and A supplied. Steady state: no fouling, no "
            "thickening, no jacket inertia, no check that the service loop can sustain it."
        ),
    )


@server.tool()
def semenov_critical_ambient(
    mass_kg: Annotated[
        float, Field(description="Mass of the contents in kg — the drum, package or vessel charge.")
    ],
    heat_release_rate_w_per_kg: Annotated[
        float,
        Field(
            description="Specific heat release rate of the decomposition in W/kg at "
            "`reference_temperature_c`, from DSC or ARC."
        ),
    ],
    reference_temperature_c: Annotated[
        float,
        Field(description="The temperature in °C at which that rate was measured."),
    ],
    activation_energy_kj_per_mol: Annotated[
        float,
        Field(description="Apparent activation energy of the decomposition, kJ/mol."),
    ],
    heat_transfer_coefficient_w_per_m2_k: Annotated[
        float,
        Field(
            description="Overall heat-loss coefficient U from the package to its surroundings, "
            "W/(m²·K). For a drum in still air this is a few W/(m²·K); it is a property of the "
            "package and its storage, and there is no default because changing it changes the "
            "answer more than anything else here."
        ),
    ],
    surface_area_m2: Annotated[
        float, Field(description="The package's external heat-loss area A, m².")
    ],
) -> SemenovResult:
    """The ambient temperature above which a package self-heats without bound — a Semenov estimate.

    **This is not an SADT.** A Self-Accelerating Decomposition Temperature is *defined* by UN Test
    Series H on a specific package in a specific size, and what this returns is a heat-balance
    estimate for deciding which test to book and at what temperature to start it. It must not be
    quoted as an SADT, entered on a transport document, or used to classify a substance.

    At the crossover the generation and loss curves touch, so their values and slopes both match:
    Q(T_c) = U·A·(T_c - T_a) together with dQ/dT = U·A. That second equation fixes T_c, and the
    first then gives T_a = T_c - R·T_c²/E_a — so the tolerable self-heating is R·T²/E_a and nothing
    else, which is why a *high* activation energy is unfavourable here.

    **The four assumptions, each a way to be wrong.** Uniform contents temperature (a well-stirred
    liquid, or a solid small enough that internal conduction is not the limit — a large solid
    package is conduction-limited and needs Frank-Kamenetskii instead); Newtonian heat loss; a
    single zero-order Arrhenius step; and no reactant consumption. A decomposition that
    autocatalyses runs away below what this returns.
    """
    balance = semenov.semenov_criticality(
        mass_kg=mass_kg,
        heat_release_rate_w_per_kg=heat_release_rate_w_per_kg,
        reference_temperature_c=reference_temperature_c,
        activation_energy_kj_per_mol=activation_energy_kj_per_mol,
        heat_transfer_coefficient_w_per_m2_k=heat_transfer_coefficient_w_per_m2_k,
        surface_area_m2=surface_area_m2,
    )
    return SemenovResult(
        critical_ambient_c=balance.critical_ambient_c,
        critical_contents_c=balance.critical_contents_c,
        self_heating_at_criticality_k=balance.self_heating_at_criticality_k,
        heat_generation_at_criticality_w=balance.heat_generation_at_criticality_w,
        rounded_up_to_five_c=semenov.round_up_to_nearest_five(balance.critical_ambient_c),
        basis=(
            "Semenov heat balance: uniform contents temperature, Newtonian loss U·A·ΔT, single "
            "zero-order Arrhenius step, no reactant consumption. An estimate for planning a UN "
            "Test Series H determination, NOT an SADT and not a transport classification."
        ),
    )


@server.tool()
def oxygen_balance_screen(
    molecular_formula: Annotated[
        str,
        Field(
            description="A plain molecular formula such as `C3H5N3O9` or `C7H5N3O6`. Not a SMILES: "
            "this server carries no cheminformatics toolkit, so a structure cannot be read. No "
            "parentheses, hydrate dots or charges — expand the composition instead, e.g. "
            "`Ca(NO3)2` as `CaN2O6`."
        ),
    ],
) -> OxygenBalanceResult:
    """Oxygen balance to CO2 — the stoichiometric screen for energetic potential.

    OB% = -1600·(2C + H/2 + 2S + metal equivalents/2 - O)/M, with each halogen tying up one hydrogen
    as HX first. A value near zero means the stoichiometry favours a rapid, complete decomposition
    *if one occurs*; a very negative one means the compound would need outside oxygen to burn.

    **It is not a hazard classification, in either direction.** OB% knows nothing about whether a
    compound decomposes at all, at what temperature, or how much energy is released: TNT is -74% and
    is an explosive, glucose is -107% and is food. A near-zero value is a reason to pursue the
    structural alerts and calorimetry, never a finding of its own; a very negative value is not a
    clearance, because an energetic group still decides the hazard.

    **For the structural half of the question, use the `safety` server**, which screens a structure
    against cited hazard-alert tables. For the energy released, use DSC — there is no tool here that
    predicts a decomposition enthalpy, and there is no corpus of measured ones.

    The molar mass and the parsed element counts come back with the number so you can check the
    parse against the formula you meant, which is the cheapest catch for a transposed subscript.
    """
    if len(molecular_formula) > MAX_FORMULA_CHARACTERS:
        raise ob.FormulaError(
            f"the formula is {len(molecular_formula)} characters; this tool accepts at most "
            f"{MAX_FORMULA_CHARACTERS}, which is far longer than any real molecular formula"
        )
    result = ob.oxygen_balance(molecular_formula)
    return OxygenBalanceResult(
        oxygen_balance_percent=result.oxygen_balance_percent,
        molar_mass_g_per_mol=result.molar_mass_g_per_mol,
        composition=result.composition,
        band=result.band,
        interpretation=result.interpretation,
        basis=(
            "Stoichiometric oxygen balance to CO2 over IUPAC 2021 atomic weights. A screening "
            "convention only: no kinetics, no decomposition energy, and no statement that the "
            "compound decomposes at all."
        ),
    )
