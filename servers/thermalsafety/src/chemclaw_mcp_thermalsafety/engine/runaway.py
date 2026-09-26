"""The runaway-scenario arithmetic: what happens to a batch if the cooling stops.

Every function here is a closed-form expression over numbers a chemist measures — a reaction
enthalpy from RC1, a decomposition onset and heat release from DSC or ARC, a jacket temperature, a
heat-transfer coefficient. **Nothing here measures anything, predicts a calorimetry trace, or
decides whether a process is safe.** It does the arithmetic that sits between a calorimetry report
and a scale-up decision, which is exactly the arithmetic people do on the back of an envelope and
get wrong under time pressure.

**Why this is a server and not a skill.** These are deterministic formulas with units, and a model
doing them in prose has no way to be checked. The formulas are textbook (Stoessel, *Thermal Safety
of Chemical Processes*, Wiley 2008; Townsend & Tou, *Thermochimica Acta* 37 (1980) 1-30 for
TMR_ad), which is what makes them safe to ship as code and unsafe to ship as recollection.

**Units are in every name and every docstring**, because that is where this arithmetic goes wrong.
A specific heat in kJ/(kg·K) against a heat release in W/kg is a factor of a thousand, and the
answer looks plausible either way.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: The gas constant, J/(mol·K). Written here rather than imported from `scipy` so that this server's
#: dependency closure stays the MCP transport and nothing else — the reason `props` gives for the
#: same choice.
GAS_CONSTANT_J_PER_MOL_K = 8.314462618

#: Absolute zero in degrees Celsius. Every public function here takes and returns Celsius, because
#: that is what a calorimetry report and a chemist both use, and converts internally.
ABSOLUTE_ZERO_C = -273.15


class ThermalInputError(ValueError):
    """An input that no arithmetic here can be run on.

    A `ValueError`, deliberately: `mcp_server_kit.connector_app` passes that family through to the
    model verbatim, because these messages are worded for a chemist and say which number is wrong.
    Anything else is replaced with an `error_id` — right for a genuine bug, wrong for "your
    accumulation fraction is above 1".
    """


def _finite(value: float, *, name: str) -> float:
    """Refuse NaN and infinity, which every comparison guard below would otherwise wave through.

    **The MCP JSON parser accepts the literals `NaN` and `Infinity`**, and `value <= 0` is False
    for both, so `adiabatic_temperature_rise` with `moles=NaN` answered `NaN` — serialised as
    `null` — and with `moles=Infinity` answered an infinite rise, where a safety number has to be a
    refusal or a number. Neither is ever a quantity somebody measured.
    """
    if not math.isfinite(value):
        raise ThermalInputError(f"{name} must be a finite number; got {value}")
    return value


def _kelvin(celsius: float, *, name: str) -> float:
    """Celsius to kelvin, refusing a temperature below absolute zero or not finite.

    Not a formality: a transposed sign on a sub-ambient jacket temperature (-20 read as -200) is a
    typo this catches, and every formula below divides by or squares a temperature in kelvin, where
    a negative value returns a confidently wrong number instead of failing.
    """
    if _finite(celsius, name=name) <= ABSOLUTE_ZERO_C:
        raise ThermalInputError(
            f"{name} is {celsius} °C, at or below absolute zero ({ABSOLUTE_ZERO_C} °C)"
        )
    return celsius - ABSOLUTE_ZERO_C


def _positive(value: float, *, name: str, unit: str) -> float:
    """A quantity that is meaningless at or below zero or not finite, refused by name."""
    if _finite(value, name=name) <= 0:
        raise ThermalInputError(f"{name} must be greater than 0 {unit}; got {value}")
    return value


def adiabatic_temperature_rise(
    *,
    heat_of_reaction_kj_per_mol: float,
    moles: float,
    mass_kg: float,
    specific_heat_kj_per_kg_k: float,
) -> float:
    """The temperature the batch would reach if every joule of reaction heat stayed in it, in K.

    ΔT_ad = |ΔH_r| · n / (m · c_p)

    The sign of `heat_of_reaction_kj_per_mol` is taken as given and its magnitude used, so an
    exothermic enthalpy written the thermodynamic way (negative) and the calorimetric way (positive)
    both give the same rise. An **endothermic** reaction has no adiabatic rise and this function is
    not the one to ask about it.

    Args:
        heat_of_reaction_kj_per_mol: Reaction enthalpy, kJ/mol of the limiting reagent. From RC1 or
            a comparable reaction calorimeter — not a computed gas-phase enthalpy, which says
            nothing about the solvated, mixed system a jacket has to remove heat from.
        moles: Moles of the limiting reagent in the batch, mol.
        mass_kg: Total mass of the reaction mass, kg — everything the heat is distributed into,
            solvent included.
        specific_heat_kj_per_kg_k: Specific heat capacity of the reaction mass, kJ/(kg·K). Around
            1.8-2.1 for common organic solvents and 4.18 for water; the mixture's value, not the
            solute's.

    Returns:
        The adiabatic temperature rise in kelvin (a difference, so identical in °C).

    Raises:
        ThermalInputError: A mass or a heat capacity at or below zero, a negative mole count, or
            any input that is not finite.
    """
    _positive(mass_kg, name="mass_kg", unit="kg")
    _positive(specific_heat_kj_per_kg_k, name="specific_heat_kj_per_kg_k", unit="kJ/(kg·K)")
    _finite(heat_of_reaction_kj_per_mol, name="heat_of_reaction_kj_per_mol")
    if _finite(moles, name="moles") < 0:
        raise ThermalInputError(f"moles must not be negative; got {moles}")
    return abs(heat_of_reaction_kj_per_mol) * moles / (mass_kg * specific_heat_kj_per_kg_k)


def mtsr(
    *,
    process_temperature_c: float,
    adiabatic_temperature_rise_k: float,
    accumulation_fraction: float,
) -> float:
    """Maximum Temperature of the Synthesis Reaction — where a cooling failure takes the batch, °C.

    MTSR = T_p + X_ac · ΔT_ad

    **The accumulation fraction is the whole safety argument and it is an input, not a default.**
    It is the fraction of the reagent that is present and unreacted at the worst moment — high for a
    reagent dosed faster than it is consumed, low for one consumed as it arrives. A process is made
    safe by *reducing accumulation*, which is why this function will not guess it: assuming 1.0
    would make every dosed process look critical, and assuming anything lower would invent the
    argument the chemist is supposed to be making.

    Args:
        process_temperature_c: The intended process temperature, °C.
        adiabatic_temperature_rise_k: ΔT_ad for the synthesis reaction, K.
        accumulation_fraction: Unreacted fraction of the limiting reagent at the worst case, 0-1.
            Measured from the calorimetric conversion curve, not assumed.

    Returns:
        MTSR in °C.

    Raises:
        ThermalInputError: An accumulation fraction outside 0-1, or a negative rise.
    """
    if not 0.0 <= accumulation_fraction <= 1.0:
        raise ThermalInputError(
            f"accumulation_fraction is a fraction between 0 and 1; got {accumulation_fraction}"
        )
    if _finite(adiabatic_temperature_rise_k, name="adiabatic_temperature_rise_k") < 0:
        raise ThermalInputError(
            f"adiabatic_temperature_rise_k must not be negative; got {adiabatic_temperature_rise_k}"
        )
    _kelvin(process_temperature_c, name="process_temperature_c")
    return process_temperature_c + accumulation_fraction * adiabatic_temperature_rise_k


def time_to_maximum_rate_hours(
    *,
    temperature_c: float,
    heat_release_rate_w_per_kg: float,
    activation_energy_kj_per_mol: float,
    specific_heat_kj_per_kg_k: float,
) -> float:
    """TMR_ad — how long an adiabatic system at this temperature takes to reach its maximum rate.

    TMR_ad = c_p · R · T² / (q(T) · E_a)

    The Townsend-Tou approximation (*Thermochimica Acta* 37 (1980) 1-30): a zero-order decomposition
    whose rate follows Arrhenius, held adiabatically. It is the number behind T_D24 — the
    temperature at which TMR_ad is 24 hours — which is the decomposition limit the Stoessel classes
    compare MTSR against.

    **What it is not.** It assumes zero-order kinetics and a single decomposition, so it is
    conservative for an autocatalytic decomposition and wrong for one with an induction period.
    Where a DSC trace shows autocatalysis, this number is optimistic and the isothermal test is the
    one that answers.

    Args:
        temperature_c: The temperature the system is held at, °C.
        heat_release_rate_w_per_kg: Specific heat release rate q at that temperature, W/kg. From an
            ARC or DSC trace read at this temperature, not extrapolated by this function.
        activation_energy_kj_per_mol: Apparent activation energy of the decomposition, kJ/mol.
        specific_heat_kj_per_kg_k: Specific heat capacity of the reaction mass, kJ/(kg·K).

    Returns:
        Time to maximum rate, in hours.

    Raises:
        ThermalInputError: A non-positive heat release rate, activation energy or heat capacity.
    """
    kelvin = _kelvin(temperature_c, name="temperature_c")
    rate = _positive(heat_release_rate_w_per_kg, name="heat_release_rate_w_per_kg", unit="W/kg")
    energy = _positive(
        activation_energy_kj_per_mol, name="activation_energy_kj_per_mol", unit="kJ/mol"
    )
    heat_capacity = _positive(
        specific_heat_kj_per_kg_k, name="specific_heat_kj_per_kg_k", unit="kJ/(kg·K)"
    )
    # Every term in SI: c_p J/(kg·K), q W/kg, E_a J/mol. The kJ inputs are what a chemist reads off
    # a report, and converting here rather than at the call site is what keeps the two from being
    # mixed — which is the failure this module's docstring names.
    seconds = (
        heat_capacity * 1000.0 * GAS_CONSTANT_J_PER_MOL_K * kelvin**2 / (rate * energy * 1000.0)
    )
    return seconds / 3600.0


def temperature_for_tmr(
    *,
    target_hours: float,
    reference_temperature_c: float,
    heat_release_rate_w_per_kg: float,
    activation_energy_kj_per_mol: float,
    specific_heat_kj_per_kg_k: float,
) -> float:
    """The temperature at which TMR_ad equals `target_hours` — T_D24 when that is 24, °C.

    Inverts `time_to_maximum_rate_hours` by extrapolating q along Arrhenius from one measured point:

        q(T) = q(T_ref) · exp( -E_a/R · (1/T - 1/T_ref) )

    then solving TMR_ad(T) = target numerically. There is no closed form (T appears squared and in
    the exponential), so this is a bisection over a bracket wide enough for anything a reactor sees.

    **This is an extrapolation and its error grows with distance from the reference point.** A q
    measured at 200 °C extrapolated to 60 °C carries the activation energy's uncertainty through an
    exponential; a T_D24 derived that way is an estimate, not a measurement, and an isothermal test
    at the temperature of interest is what settles it. The answer is returned all the same, because
    the alternative in practice is the same extrapolation done in somebody's head.

    Args:
        target_hours: The TMR_ad to solve for, hours. 24 gives T_D24.
        reference_temperature_c: The temperature `heat_release_rate_w_per_kg` was read at, °C.
        heat_release_rate_w_per_kg: Specific heat release rate at the reference temperature, W/kg.
        activation_energy_kj_per_mol: Apparent activation energy, kJ/mol.
        specific_heat_kj_per_kg_k: Specific heat capacity, kJ/(kg·K).

    Returns:
        The temperature in °C at which TMR_ad equals `target_hours`.

    Raises:
        ThermalInputError: A non-positive input, or a target no temperature in -100..500 °C reaches
            — which is itself the answer, and says so.
    """
    _positive(target_hours, name="target_hours", unit="h")
    reference_kelvin = _kelvin(reference_temperature_c, name="reference_temperature_c")
    reference_rate = _positive(
        heat_release_rate_w_per_kg, name="heat_release_rate_w_per_kg", unit="W/kg"
    )
    energy = _positive(
        activation_energy_kj_per_mol, name="activation_energy_kj_per_mol", unit="kJ/mol"
    )
    heat_capacity = _positive(
        specific_heat_kj_per_kg_k, name="specific_heat_kj_per_kg_k", unit="kJ/(kg·K)"
    )

    def tmr_at(celsius: float) -> float:
        kelvin = celsius - ABSOLUTE_ZERO_C
        rate = reference_rate * math.exp(
            -(energy * 1000.0) / GAS_CONSTANT_J_PER_MOL_K * (1.0 / kelvin - 1.0 / reference_kelvin)
        )
        return (
            heat_capacity * 1000.0 * GAS_CONSTANT_J_PER_MOL_K * kelvin**2 / (rate * energy * 1000.0)
        ) / 3600.0

    # TMR falls as temperature rises, so the bracket runs cold (long TMR) to hot (short TMR).
    low, high = -99.0, 500.0
    if tmr_at(low) < target_hours:
        raise ThermalInputError(
            f"TMR_ad is already below {target_hours} h at {low} °C, so no temperature in this "
            "range reaches it — the decomposition is fast everywhere a reactor operates, which is "
            "the finding rather than a failure of this calculation"
        )
    if tmr_at(high) > target_hours:
        raise ThermalInputError(
            f"TMR_ad is still above {target_hours} h at {high} °C; this decomposition does not "
            "reach that rate in any range a reactor operates in"
        )
    for _ in range(200):
        middle = (low + high) / 2.0
        if tmr_at(middle) > target_hours:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


@dataclass(frozen=True)
class Criticality:
    """A Stoessel criticality class and the ordering that produced it."""

    #: 1 to 5. Lower is less critical; see `stoessel_class` for what each means.
    criticality_class: int
    #: The four temperatures in ascending order, as `("T_p", 20.0)` pairs — the actual evidence for
    #: the class, so a reader can see *why* rather than take the number on trust.
    ordering: tuple[tuple[str, float], ...]
    #: What the class means for this process, in the words the classification uses.
    interpretation: str


def stoessel_class(
    *,
    process_temperature_c: float,
    mtsr_c: float,
    max_technical_temperature_c: float,
    decomposition_t_d24_c: float,
) -> Criticality:
    """Classify a runaway scenario 1-5 by the order of its four characteristic temperatures.

    Stoessel's criticality classes (*Thermal Safety of Chemical Processes*, Wiley 2008, ch. 3). The
    class is decided entirely by where MTSR falls relative to the maximum technical temperature
    (MTT — the boiling point, or the pressure-relief set point for a closed system) and T_D24:

    - **1** — `T_p < MTSR < MTT < T_D24`. The runaway reaches neither boiling nor decomposition.
    - **2** — `T_p < MTSR < T_D24 < MTT`. As 1; the order of the two barriers differs but neither
      is crossed.
    - **3** — `T_p < MTT < MTSR < T_D24`. The runaway boils the batch before it decomposes. The
      solvent is the safety barrier, and it only works if the vapour can be handled.
    - **4** — `T_p < MTT < T_D24 < MTSR`. Decomposition is reached, with boiling on the way; the
      evaporative cooling is what stands between the two, and it has to be shown to be enough.
    - **5** — `T_p < T_D24 < MTT < MTSR`. Decomposition is reached with no barrier before it. The
      most critical case.

    **This classifies a scenario; it does not approve or reject a process.** Classes 1 and 2 are not
    "safe" — they say a cooling failure does not reach decomposition, which is one scenario among
    the several a hazard study covers, and says nothing about dosing errors, wrong charges, mixing
    failure or the accumulated reagent's own stability.

    Args:
        process_temperature_c: Intended process temperature, °C.
        mtsr_c: Maximum temperature of the synthesis reaction, °C — from `mtsr`.
        max_technical_temperature_c: MTT, °C. The boiling point at the operating pressure for an
            open system; the relief set point for a closed one.
        decomposition_t_d24_c: T_D24, °C — the temperature at which TMR_ad is 24 h.

    Returns:
        The class, the ordering behind it, and what it means.

    Raises:
        ThermalInputError: MTSR below the process temperature, which is not a runaway scenario.
    """
    for name, value in (
        ("process_temperature_c", process_temperature_c),
        ("mtsr_c", mtsr_c),
        ("max_technical_temperature_c", max_technical_temperature_c),
        ("decomposition_t_d24_c", decomposition_t_d24_c),
    ):
        _kelvin(value, name=name)
    if mtsr_c < process_temperature_c:
        raise ThermalInputError(
            f"MTSR ({mtsr_c} °C) is below the process temperature ({process_temperature_c} °C). "
            "MTSR is where a cooling failure takes the batch, so it cannot be colder than where "
            "the batch already is — check the accumulation fraction and the adiabatic rise"
        )
    ordering = tuple(
        sorted(
            (
                ("T_p", process_temperature_c),
                ("MTSR", mtsr_c),
                ("MTT", max_technical_temperature_c),
                ("T_D24", decomposition_t_d24_c),
            ),
            key=lambda pair: pair[1],
        )
    )
    if mtsr_c < max_technical_temperature_c and mtsr_c < decomposition_t_d24_c:
        criticality = 1 if max_technical_temperature_c < decomposition_t_d24_c else 2
    elif mtsr_c < decomposition_t_d24_c:
        criticality = 3
    elif max_technical_temperature_c < decomposition_t_d24_c:
        criticality = 4
    else:
        criticality = 5
    return Criticality(
        criticality_class=criticality,
        ordering=ordering,
        interpretation=_INTERPRETATION[criticality],
    )


_INTERPRETATION: dict[int, str] = {
    1: (
        "A cooling failure reaches neither the boiling point nor the decomposition temperature. "
        "The least critical ordering — which is not the same as a safe process, only a bounded "
        "cooling-failure scenario."
    ),
    2: (
        "A cooling failure reaches neither barrier. As class 1, with the boiling point above the "
        "decomposition limit rather than below it, so evaporative cooling is not the barrier in "
        "reserve."
    ),
    3: (
        "A cooling failure boils the batch before it can decompose. The solvent is the safety "
        "barrier: it works only if the vapour rate at MTSR can actually be handled — condenser "
        "duty, vent line and scrubber — which this classification does not check."
    ),
    4: (
        "A cooling failure reaches the decomposition temperature, with boiling on the way. "
        "Evaporative cooling is what stands between the two and has to be shown sufficient; "
        "otherwise this behaves as class 5."
    ),
    5: (
        "A cooling failure reaches the decomposition temperature with no barrier before it. The "
        "most critical ordering: the scenario has to be eliminated by process design — lower "
        "accumulation, lower charge, a different solvent — rather than mitigated."
    ),
}


def heat_removal_capacity(
    *,
    heat_transfer_coefficient_w_per_m2_k: float,
    heat_exchange_area_m2: float,
    reactor_temperature_c: float,
    jacket_temperature_c: float,
    mass_kg: float,
) -> tuple[float, float]:
    """What the jacket can take out, as a total and per kilogram of reaction mass.

    q_ex = U · A · (T_r - T_j), and q_ex/m for comparison against a specific heat release rate.

    **The sign is kept.** A jacket warmer than the reactor removes nothing and this returns a
    negative number rather than an absolute value, because "the cooling is a heat source right now"
    is a real and reportable state — a batch being warmed to its process temperature is in it.

    **What this is not.** A steady-state capacity at one instant, from a U somebody measured or
    estimated. It is not a dynamic model: it says nothing about how U falls as a batch thickens,
    about fouling, about the jacket's own thermal inertia, or about whether the cooling medium can
    sustain that duty. The classic scale-up failure is in none of those terms — it is that A/V falls
    as volume rises, so a duty per kilogram that was ample in a 1 L flask is not in a 1 m³ reactor.

    Args:
        heat_transfer_coefficient_w_per_m2_k: Overall U, W/(m²·K).
        heat_exchange_area_m2: Wetted heat-exchange area A, m².
        reactor_temperature_c: Reaction mass temperature, °C.
        jacket_temperature_c: Jacket (service fluid) temperature, °C.
        mass_kg: Reaction mass, kg.

    Returns:
        `(total_w, specific_w_per_kg)` — the duty in W and in W/kg.

    Raises:
        ThermalInputError: A non-positive U, area or mass.
    """
    coefficient = _positive(
        heat_transfer_coefficient_w_per_m2_k,
        name="heat_transfer_coefficient_w_per_m2_k",
        unit="W/(m²·K)",
    )
    area = _positive(heat_exchange_area_m2, name="heat_exchange_area_m2", unit="m²")
    mass = _positive(mass_kg, name="mass_kg", unit="kg")
    _kelvin(reactor_temperature_c, name="reactor_temperature_c")
    _kelvin(jacket_temperature_c, name="jacket_temperature_c")
    total = coefficient * area * (reactor_temperature_c - jacket_temperature_c)
    return total, total / mass
