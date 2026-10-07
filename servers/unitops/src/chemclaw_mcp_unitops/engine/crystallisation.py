"""Crystallisation yield from two points on a solubility curve, by mass balance.

    solute in the liquor at the end = S_cold · (solvent charged - solvent evaporated)
    crystals = solute charged - solute in the liquor

Arithmetic on solubilities the chemist supplies, not a solubility model: a yield from a guessed
solubility is a guess. The balance assumes equilibrium at the final temperature (real liquors stay
supersaturated, so real yields are lower), an anhydrous non-solvated solid, no oiling out, inclusion
or losses, and solubility unaffected by impurities. So it is the **maximum** yield the two
solubilities permit.
"""

from __future__ import annotations

from dataclasses import dataclass

from chemclaw_mcp_unitops.engine.validation import UnitOpsInputError, non_negative, positive

__all__ = ["CrystallisationYield", "crystallisation_yield"]


@dataclass(frozen=True)
class CrystallisationYield:
    """What the balance permits, and what stays behind."""

    crystal_mass_kg: float
    #: Solute still dissolved in the filtered mother liquor, kg; usually the largest single
    #: isolation loss.
    mother_liquor_loss_kg: float
    #: Crystals divided by the solute charged, 0 to 1.
    yield_fraction: float
    yield_percent: float
    #: Solvent remaining at the end, in kg, after any evaporation.
    solvent_at_end_kg: float
    #: Solute charged over what the hot solvent could hold. 1.0 is exactly saturated, the normal
    #: design start; well below it, dilution throws yield away.
    saturation_at_start: float


def crystallisation_yield(
    *,
    solute_charged_kg: float,
    solvent_charged_kg: float,
    solubility_hot_kg_per_kg_solvent: float,
    solubility_cold_kg_per_kg_solvent: float,
    solvent_evaporated_kg: float = 0.0,
) -> CrystallisationYield:
    """The maximum yield two solubilities permit, by mass balance.

    Args:
        solute_charged_kg: Product dissolved at the hot temperature, kg (the solute, not the crude
        charge). solvent_charged_kg: Solvent at the hot temperature, kg. **Mass, not volume** (5 L
        of isopropanol is 3.93 kg). solubility_hot_kg_per_kg_solvent: Measured solubility at the
        starting temperature, kg solute per kg solvent. solubility_cold_kg_per_kg_solvent:
        Solubility at the final temperature, same units; the yield is almost entirely this number.
        solvent_evaporated_kg: Solvent removed during the operation, kg; zero for straight cooling.

    Returns:
        The crystal mass, the liquor loss, the yield and how saturated the charge was to begin with.

    Raises:
        UnitOpsInputError: A mass or solubility is not positive, evaporation removes all the
        solvent, the cold solubility is not below the hot one (no driving force), or the charge
        exceeds what the hot solvent dissolves.
    """
    positive(solute_charged_kg, "the solute charged")
    positive(solvent_charged_kg, "the solvent charged")
    positive(solubility_hot_kg_per_kg_solvent, "the solubility at the hot temperature")
    positive(solubility_cold_kg_per_kg_solvent, "the solubility at the cold temperature")
    non_negative(solvent_evaporated_kg, "the solvent evaporated")

    if solubility_cold_kg_per_kg_solvent >= solubility_hot_kg_per_kg_solvent:
        raise UnitOpsInputError(
            f"the cold solubility ({solubility_cold_kg_per_kg_solvent} kg/kg) is not below the hot "
            f"one ({solubility_hot_kg_per_kg_solvent} kg/kg), so cooling creates no driving force "
            "and nothing crystallises. If those are the measured numbers, this compound does not "
            "have a cooling crystallisation in this solvent — an anti-solvent or a distillative "
            "concentration is the operation, and neither is this balance."
        )

    solvent_at_end = solvent_charged_kg - solvent_evaporated_kg
    if solvent_at_end <= 0.0:
        raise UnitOpsInputError(
            f"evaporating {solvent_evaporated_kg} kg removes all {solvent_charged_kg} kg of "
            "solvent. What is left is a dry-down rather than a crystallisation, and its yield is "
            "not set by a solubility."
        )

    capacity_hot = solubility_hot_kg_per_kg_solvent * solvent_charged_kg
    saturation = solute_charged_kg / capacity_hot
    if saturation > 1.0:
        raise UnitOpsInputError(
            f"{solute_charged_kg} kg of solute in {solvent_charged_kg} kg of solvent is "
            f"{saturation:.2f} times what the hot solubility "
            f"({solubility_hot_kg_per_kg_solvent} kg/kg) can dissolve, so part of the charge never "
            "goes into solution. This balance assumes a clear solution at the start; a slurry that "
            "was never fully dissolved is a different operation and its 'yield' would include "
            "material that never crystallised."
        )

    in_liquor = solubility_cold_kg_per_kg_solvent * solvent_at_end
    crystals = solute_charged_kg - in_liquor
    if crystals <= 0.0:
        # Not an error: a charge well below saturation yields nothing on cooling, and that is the
        # answer.
        return CrystallisationYield(
            crystal_mass_kg=0.0,
            mother_liquor_loss_kg=solute_charged_kg,
            yield_fraction=0.0,
            yield_percent=0.0,
            solvent_at_end_kg=solvent_at_end,
            saturation_at_start=saturation,
        )

    return CrystallisationYield(
        crystal_mass_kg=crystals,
        mother_liquor_loss_kg=in_liquor,
        yield_fraction=crystals / solute_charged_kg,
        yield_percent=100.0 * crystals / solute_charged_kg,
        solvent_at_end_kg=solvent_at_end,
        saturation_at_start=saturation,
    )
