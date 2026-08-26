"""Assigning a role to every species of one reaction.

Three sources of evidence, used in this order because that is the order of how much each one
actually knows:

1. **Which slot the species is written in.** `reactants>agents>products` already separates products
   from everything else, and it is the one fact the extractor was certain about.
2. **The atom map**, where a mapper is installed. A species in the reactants slot that contributes
   *no atoms* to the product did not become the product — it is a reagent, a base, an oxidant. This
   is the classical reactant-versus-reagent split (Schneider, Lowe, Sayle and Landrum, "What's
   What", JCIM 2016), and it is the only one of the three that can tell a stoichiometric reductant
   from a substrate.
3. **The structure rules** in `agents.py`, which turn "not a substrate" into *which* kind of
   not-a-substrate: catalyst, ligand, base, solvent, additive.

**Without a mapper this still works, and says so.** Step 2 is skipped, so a reactant-slot species is
a starting material unless a structure rule claims it. That is a genuine loss of resolution — a
stoichiometric reductant written on the left will read as a substrate — and it is recorded in
`labeller_version`, so every row labelled that way goes stale the moment a deployment installs the
mapper. The alternative, refusing to label without a transformer, would leave a corpus with no roles
at all rather than one with slightly coarse ones.
"""

from __future__ import annotations

import logging

from rdkit import Chem

from chemclaw_mcp_rxnlabel.engine import agents, mapping
from chemclaw_mcp_rxnlabel.engine.chem import read_molecule

logger = logging.getLogger(__name__)

# The vocabulary, matching `chemclaw.science.labels.vocabulary.SpeciesRole` on the other side of the
# wire. Strings rather than an enum shared across two repositories: the wire contract is JSON, and a
# shared enum would be a package one of the two would have to depend on. The client is lenient about
# a value it does not know (it degrades to `unknown`), so adding one here is a version bump rather
# than a breaking change.
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
        species: The structures to classify, in the caller's own order. Sent explicitly rather
            than parsed out of the reaction because the caller's ordinals come from its own record
            and the reaction string groups the agents together — the two orders differ on every
            reaction with a solvent, so matching by position would mislabel all of them.
        mapped: The atom-mapped form from `mapping.map_reaction`, or `None` where there is no
            mapper. Passed in rather than derived here because the caller stores that string as
            well, and mapping it in both places ran the transformer twice per reaction.

    Returns:
        One role per input species. A species that appears in no slot is `unknown`, which is a real
        answer: it means the caller's record and the reaction string disagree, and inventing a role
        for it would hide that.
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

    # Step 3 first for the agent slot, and for a reactant the atom map has demoted. The structure
    # rules are consulted in order of how specific they are: a metal is a catalyst whatever else it
    # is, a donor motif is a ligand only where there is a metal to bind, and a tertiary amine is a
    # base only once we already know it is not the substrate.
    demoted = in_reactants and contributing is not None and not _in_slot(canonical, contributing)
    if in_reactants and not demoted:
        # A substrate. Nothing below applies: a substrate that happens to be an amine is not a
        # base, and this guard is the whole reason `is_base`'s last rule is safe to have.
        return STARTING_MATERIAL
    if agents.is_ligand(canonical, context):
        # Before the metal check, deliberately: dppf and other ferrocenyl phosphines *contain* a
        # transition metal and are ligands, not catalysts.
        return LIGAND
    if agents.is_metal_complex(canonical):
        return CATALYST
    if agents.is_solvent(canonical):
        return SOLVENT
    if agents.is_base(canonical):
        return BASE
    # Everything else in the agent slot is an additive; everything else demoted out of the reactant
    # slot is a reagent. The distinction is where it was written, which is the one thing the
    # extractor was sure of: something charged on the left participates stoichiometrically.
    return REAGENT if in_reactants else ADDITIVE


def _in_slot(canonical: str, slot: set[str]) -> bool:
    """Whether a species — possibly a multi-component one — is written in this slot.

    **Component-wise, and that is not a nicety.** A slot is a dot-joined list, and so is a salt, a
    solvate or a metal complex: `[Fe+2].c1ccc(P(...)...)cc1` is *one* species the caller charged and
    two dot-separated tokens in the reaction string. Matching the whole string against the slot's
    tokens finds neither, and the species comes back `unknown` — which is what happened to every
    ferrocenyl phosphine and every alkali-metal salt until a test on dppf caught it.

    So a species belongs to a slot when every one of its components is written there. That is right
    in both directions: a single-component species reduces to plain membership, and a two-component
    complex is not claimed by a slot that holds only one of its halves.
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

    Skipping rather than failing: a patent extract's fiftieth species may be an OCR artefact, and
    losing the roles of the other forty-nine over it is a worse answer than losing that one's.
    """
    found = set()
    for token in slot.split("."):
        canonical = _canonical(token)
        if canonical is not None:
            found.add(canonical)
    return found


def _canonical(smiles: str) -> str | None:
    """RDKit's canonical form, or `None`.

    Both sides of every comparison in this module go through here, which is the point: the caller's
    standardisation is not this server's, so matching raw strings would fail on the difference
    between two spellings of one molecule and silently return `unknown` for a species that is
    plainly there.

    Read *whole* or not at all (`engine/chem.py`): a truncated parse would put a smaller molecule on
    one side of that comparison, which is a mismatch dressed as a match.
    """
    mol = read_molecule(smiles)
    return Chem.MolToSmiles(mol) if mol is not None else None
