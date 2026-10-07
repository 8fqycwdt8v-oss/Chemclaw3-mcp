"""Assigning a role to every species of one reaction.

Three sources of evidence, in order of how much each knows:

1. **The slot** a species is written in (`reactants>agents>products`).
2. **The atom map**, where a mapper is installed: a reactant-slot species contributing no atoms to
   the product is a reagent (Schneider et al., "What's What", JCIM 2016).
3. **The structure rules** in `agents.py`: catalyst, ligand, base, solvent, additive.

Without a mapper step 2 is skipped, so a stoichiometric reagent written on the left reads as a
substrate. That coarser answer is recorded in `labeller_version`, so those rows go stale once a
mapper is installed.
"""

from __future__ import annotations

import logging

from rdkit import Chem

from chemclaw_mcp_rxnlabel.engine import agents, mapping
from chemclaw_mcp_rxnlabel.engine.chem import read_molecule

logger = logging.getLogger(__name__)

# The vocabulary, matching `chemclaw.science.labels.vocabulary.SpeciesRole` across the wire. Plain
# strings because the contract is JSON; the client degrades an unknown value to `unknown`, so adding
# one is a version bump.
STARTING_MATERIAL = "starting-material"
PRODUCT = "product"
REAGENT = "reagent"
SOLVENT = "solvent"
CATALYST = "catalyst"
LIGAND = "ligand"
BASE = "base"
ADDITIVE = "additive"
UNKNOWN = "unknown"


def assign(reaction_smiles: str, species: list[str], mapped: str | None) -> list[str]:
    """A role for each species, positionally against the list given.

    Args:
        reaction_smiles: The record form, `reactants>agents>products`, with the agents kept.
        species: The structures to classify, in the caller's own order (which differs from the
            reaction string's, so it is sent explicitly).
        mapped: The atom-mapped form from `mapping.map_reaction`, or `None` where there is no
            mapper.

    Returns:
        One role per input species. A species in no slot is `unknown`: the record and the reaction
            string disagree, and inventing a role would hide that.
    """
    slots = _slots(reaction_smiles)
    if slots is None:
        return [UNKNOWN] * len(species)
    reactants, agent_slot, products = slots
    context = agents.context_of([*reactants, *agent_slot])
    contributing = mapping.contributing_reactants(mapped)
    return [_role(s, reactants, agent_slot, products, context, contributing) for s in species]


def _role(
    smiles: str,
    reactants: set[str],
    agent_slot: set[str],
    products: set[str],
    context: agents.ReactionContext,
    contributing: set[str] | None,
) -> str:
    """One species' role, by the three-step argument in the module docstring."""
    canonical = _canonical(smiles)
    if canonical is None:
        return UNKNOWN
    if _in_slot(canonical, products):
        return PRODUCT
    in_reactants = _in_slot(canonical, reactants)
    if not in_reactants and not _in_slot(canonical, agent_slot):
        return UNKNOWN

    # Structure rules for the agent slot and for map-demoted reactants, most specific first: a metal
    # is a catalyst, a donor motif is a ligand only with a metal present, an amine is a base only
    # once known not to be the substrate.
    demoted = in_reactants and contributing is not None and not _in_slot(canonical, contributing)
    if in_reactants and not demoted:
        # A substrate; this guard is what makes `is_base`'s tertiary-amine rule safe.
        return STARTING_MATERIAL
    if agents.is_ligand(canonical, context):
        # Before the metal check: ferrocenyl phosphines contain a metal but are ligands.
        return LIGAND
    if agents.is_metal_complex(canonical):
        return CATALYST
    if agents.is_solvent(canonical):
        return SOLVENT
    if agents.is_base(canonical):
        return BASE
    # Unclassified agents are additives; unclassified demoted reactants are reagents (charged on the
    # left, so stoichiometric).
    return REAGENT if in_reactants else ADDITIVE


def _in_slot(canonical: str, slot: set[str]) -> bool:
    """Whether a species — possibly a multi-component one — is written in this slot.

    Component-wise: a salt or complex is one species but several dot-separated tokens, so it belongs
    to a slot when every component is written there.
    """
    return all(part in slot for part in canonical.split("."))


def _slots(reaction_smiles: str) -> tuple[set[str], set[str], set[str]] | None:
    """The three slots as sets of canonical SMILES, or `None` if this is not a reaction."""
    parts = reaction_smiles.split(">")
    if len(parts) != 3:
        return None
    return tuple(_canonical_set(part) for part in parts)  # type: ignore[return-value]


def _canonical_set(slot: str) -> set[str]:
    """One slot's species, canonicalised, skipping what RDKit cannot read.

    Skipping so one OCR artefact does not lose the other species' roles.
    """
    found = set()
    for token in slot.split("."):
        canonical = _canonical(token)
        if canonical is not None:
            found.add(canonical)
    return found


def _canonical(smiles: str) -> str | None:
    """RDKit's canonical form, or `None`.

    Both sides of every comparison here go through it, so two spellings of one molecule match. Read
    whole or not at all (`engine/chem.py`), so a truncated parse cannot produce a false match.
    """
    mol = read_molecule(smiles)
    return Chem.MolToSmiles(mol) if mol is not None else None
