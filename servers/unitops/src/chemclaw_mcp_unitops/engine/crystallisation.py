"""Crystallisation yield from two points on a solubility curve, by mass balance.

The whole of it is one balance. What crystallises is what the cold liquor cannot hold:

    solute in the liquor at the end = S_cold · (solvent charged - solvent evaporated)
    crystals = solute charged - solute in the liquor

**This is arithmetic on solubilities the chemist supplies, and it is not a solubility model.** There
is no solubility curve anywhere in this system, and the two numbers are the answer's entire content:
a yield computed from a guessed solubility is a guess wearing a percentage sign. Chemclaw3's own
aqueous predictor is not a substitute — a water solubility used for an isopropanol crystallisation
is the near-miss its probe set names explicitly.

**What the balance assumes, each of which moves a real yield away from this number.**

- *Equilibrium at the final temperature.* A real cooling crystallisation stops somewhere inside the
  metastable zone, so the liquor is supersaturated when it is filtered and the yield is **lower**
  than this. How much lower is a kinetics question — cooling rate, seeding, agitation — that no
  balance answers.
- *An anhydrous solid of the same substance.* A hydrate or a solvate takes solvent out of the liquor
  with it and adds its own mass to the cake, so both halves of the balance move.
- *No oiling out, no inclusion, no losses to the vessel or the cake wash.*
- *The solubility is unaffected by what else is in the liquor.* Impurities, a reaction by-product or
  residual base routinely change it, in either direction.

So this is the **maximum** yield the two solubilities permit, and the gap between it and a real
batch is where crystallisation development happens.
"""

from __future__ import annotations

from dataclasses import dataclass

from chemclaw_mcp_unitops.engine.validation import UnitOpsInputError, non_negative, positive

__all__ = ["CrystallisationYield", "crystallisation_yield"]


@dataclass(frozen=True)
class CrystallisationYield:
    """What the balance permits, and what stays behind."""

    crystal_mass_kg: float
    #: What is dissolved in the mother liquor when it is filtered — the loss this calculation
    #: exists to quantify, since it is usually the largest single one in an isolation.
    mother_liquor_loss_kg: float
    #: Crystals divided by the solute charged, 0 to 1.
    yield_fraction: float
    yield_percent: float
    #: Solvent remaining at the end, in kg, after any evaporation.
    solvent_at_end_kg: float
    #: How saturated the charge was at the hot end — the solute charged over what the hot solvent
    #: could hold. At 1.0 the charge is exactly saturated, which is where a cooling crystallisation
    #: is normally designed to start; well below it, the cooling curve is being run at a dilution
    #: that throws the yield away before it starts.
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
        solute_charged_kg: Product dissolved at the hot temperature, in kg. The solute, not the
            slurry and not the crude charge including impurities.
        solvent_charged_kg: Solvent in the vessel at the hot temperature, in kg. **Mass, not
            volume** — 5 L of isopropanol is 3.93 kg, and using the litres directly overstates the
            liquor loss by a quarter.
        solubility_hot_kg_per_kg_solvent: Solubility at the starting temperature, in kg of solute
            per kg of solvent. A measured number, from this solvent system at this temperature.
        solubility_cold_kg_per_kg_solvent: Solubility at the final temperature, same units. This is
            the number the yield is almost entirely made of.
        solvent_evaporated_kg: Solvent removed during the operation, in kg. Zero for a straight
            cooling crystallisation; non-zero for a concentrate-and-cool or a distillative swap.

    Returns:
        The crystal mass, the liquor loss, the yield and how saturated the charge was to begin with.

    Raises:
        UnitOpsInputError: If a mass or solubility is not positive, if the evaporation removes all
            the solvent, if the cold solubility is not below the hot one (there is no driving
            force, so nothing crystallises and the answer is not a small yield but no operation),
            or if the charge exceeds what the hot solvent can dissolve — because then part of it
            never went into solution and this balance describes a different experiment.
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
        # Not an error: a charge well below saturation legitimately yields nothing on cooling, and
        # that is the answer the chemist needs — the batch is too dilute, rather than the tool
        # being unable to say so.
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
