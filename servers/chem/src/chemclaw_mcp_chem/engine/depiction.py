"""Draw a molecule or a reaction as an SVG — the most expensive thing this server does.

2D-coordinate generation and rasterising to SVG are CPU-bound C++ that holds the GIL for tens of
milliseconds on a drug-sized molecule. Chemclaw3 measured the consequence of doing that on the
event loop: throughput flat from 10 to 50 concurrent users. So `tools.py` awaits this in a worker
thread — RDKit releases the GIL for the heavy passes, so the threads are real parallelism.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from mcp_server_kit.limits import smiles_length_error
from rdkit import Chem
from rdkit.Chem import Draw, rdChemReactions
from rdkit.Chem.Draw import rdMolDraw2D

from chemclaw_mcp_chem.engine.chem import (
    InvalidSmilesError,
    require_molecule,
    require_whole_string,
)

__all__ = ["RENDER_SIZE_PX", "render_svg"]

# Edge length of a rendered depiction, in pixels; a reaction is drawn twice as wide as it is tall.
#
# Chemclaw3 carries this as `settings.structure_render_size_px`, default 320, for a reason that
# survives the port: a deployment whose chat surface renders larger cards must be able to change it
# without a code edit. This server has no settings object of its own — one integer does not earn a
# pydantic-settings dependency — so the same knob is one environment variable, read once at import
# and prefixed like every other variable this fleet reads.
RENDER_SIZE_PX = int(os.environ.get("CHEMCLAW_CHEM_RENDER_SIZE_PX", "320"))

# The largest molecule (or whole reaction) this server will lay out and draw.
#
# `Compute2DCoords` is not linear: it embeds the molecular graph, and the cost of that embedding
# climbs far faster than the atom count. Measured on the installed RDKit, a ~1500-atom linear
# molecule (a 6 KB request of `"C" * 6000`) took **672 s** of one worker thread — a single
# authenticated call that pins a core for eleven minutes, and `render_structure` offloads to a
# thread whose cancellation does not stop it, so the caller's timeout frees nothing. The bound sits
# *before* `Compute2DCoords`, since that is the call that runs away. A depiction is also useless far
# below this: 250 atoms in a 320 px square is an unreadable tangle, so the limit costs no real
# drawing. Config, not a constant, so a deployment rendering poster-size cards can raise it.
MAX_DEPICTION_ATOMS = int(os.environ.get("CHEMCLAW_CHEM_MAX_DEPICTION_ATOMS", "250"))


def render_svg(smiles: str, highlight_atoms: Sequence[int] | None = None) -> str:
    """An inline SVG document depicting `smiles`, which may be a molecule or a reaction.

    A reaction is anything containing `>>`; everything else goes through the same strict parse the
    rest of this server uses, so a string RDKit would silently truncate to a smaller molecule is
    refused rather than drawn. Chemclaw3 canonicalized and re-parsed before drawing; that second
    parse only reorders atoms, and dropping it is one parse fewer for an identical picture.

    **`highlight_atoms` is how a choice of bond becomes checkable by a human** rather than merely
    stated. A torsion is named by four atom indices nobody can see, and the failure mode of getting
    it wrong is a plausible answer about a different bond; drawing the chosen atoms — and the bonds
    between them — puts the choice in front of the chemist in the one form they can verify at a
    glance. Out-of-range indices are refused rather than ignored, because a highlight that silently
    did not appear would confirm nothing while looking like confirmation.

    Raises:
        InvalidSmilesError: the string is not a drawable molecule or reaction, or an index does not
            address one of its atoms.
    """
    # The whole-string guard runs before the reaction branch, not inside `require_molecule` where
    # only the molecule path would reach it — see `require_whole_string`. The length bound runs
    # first so a megastring never reaches the reaction parser (the molecule path re-checks in
    # `require_molecule`, harmlessly).
    kind = "reaction SMILES" if ">>" in smiles else "SMILES"
    if reason := smiles_length_error(smiles, subject=f"this {kind}"):
        raise InvalidSmilesError(reason)
    smiles = require_whole_string(smiles, kind)
    if ">>" in smiles:
        if highlight_atoms:
            raise InvalidSmilesError("a reaction drawing takes no atom highlight")
        reaction = _reaction(smiles)
        _refuse_if_too_large(_reaction_atoms(reaction), "this reaction")
        drawer = rdMolDraw2D.MolDraw2DSVG(RENDER_SIZE_PX * 2, RENDER_SIZE_PX)
        drawer.DrawReaction(reaction)
    else:
        mol = require_molecule(smiles)
        _refuse_if_too_large(mol.GetNumAtoms(), "this molecule")
        # Compute 2D coordinates so the depiction is laid out, not collapsed on the origin.
        Draw.rdDepictor.Compute2DCoords(mol)
        drawer = rdMolDraw2D.MolDraw2DSVG(RENDER_SIZE_PX, RENDER_SIZE_PX)
        atoms = _checked_atoms(mol, highlight_atoms)
        drawer.DrawMolecule(mol, highlightAtoms=atoms, highlightBonds=_spanned_bonds(mol, atoms))
    drawer.FinishDrawing()
    return str(drawer.GetDrawingText())


def _refuse_if_too_large(num_atoms: int, subject: str) -> None:
    """Refuse a depiction above `MAX_DEPICTION_ATOMS`, *before* the coordinate embedding runs.

    `Compute2DCoords` is superlinear, so this is the guard that stops one request pinning a core
    for minutes. The message is a worded `InvalidSmilesError` (a `ValueError`), so it reaches the
    model verbatim rather than as an internal-error notice.
    """
    if num_atoms > MAX_DEPICTION_ATOMS:
        raise InvalidSmilesError(
            f"{subject} has {num_atoms} atoms, above the {MAX_DEPICTION_ATOMS}-atom depiction "
            "limit. Laying out a graph this large is disproportionately expensive and the picture "
            "would be an unreadable tangle at this size; raise CHEMCLAW_CHEM_MAX_DEPICTION_ATOMS "
            "to draw it anyway."
        )


def _reaction_atoms(reaction: rdChemReactions.ChemicalReaction) -> int:
    """The total heavy-atom count across every reactant and product template of a reaction.

    `DrawReaction` lays out every component, so the runaway cost is bounded by the whole reaction's
    atoms rather than any single component's.
    """
    templates = list(reaction.GetReactants()) + list(reaction.GetProducts())
    return sum(int(template.GetNumAtoms()) for template in templates)


def _checked_atoms(mol: Chem.Mol, highlight_atoms: Sequence[int] | None) -> list[int]:
    """The highlight indices, refusing any that does not address an atom of this molecule."""
    atoms = list(highlight_atoms or ())
    if out_of_range := [index for index in atoms if not 0 <= index < mol.GetNumAtoms()]:
        raise InvalidSmilesError(
            f"atom indices {out_of_range} are not atoms of a {mol.GetNumAtoms()}-atom molecule"
        )
    return atoms


def _spanned_bonds(mol: Chem.Mol, atoms: Sequence[int]) -> list[int]:
    """Every bond whose two ends are both highlighted — the chain of a torsion, drawn as a chain."""
    chosen = set(atoms)
    return [
        bond.GetIdx()
        for bond in mol.GetBonds()
        if bond.GetBeginAtomIdx() in chosen and bond.GetEndAtomIdx() in chosen
    ]


def _reaction(smiles: str) -> rdChemReactions.ChemicalReaction:
    """Parse a reaction SMILES, or raise `InvalidSmilesError` naming the string that was refused.

    Two things about `ReactionFromSmarts` were measured against the installed RDKit rather than read
    off Chemclaw3's version of this code, and both change what has to be written here:

    - **It raises rather than returning `None`.** `"a>>b"` and `"CCO junk>>CC=O"` come back as a
      bare `ValueError` carrying RDKit's own `ChemicalReactionParserException` wording. That is a
      `ValueError`, so `connector_app` would pass it to the model verbatim — an internal parser
      message where the tool means "this is not a reaction I can draw". Hence the translation.
    - **It accepts an empty reaction.** `">>"` parses to zero reactants and zero products and draws
      a blank picture, which is the reaction form of the empty SMILES `require_molecule` refuses.

    The non-ASCII case is the third and the quiet one: RDKit skips a run of non-ASCII bytes at a
    component's edges, so `"°C>>CC=O"` parses — as *methane* reacting to acetaldehyde. Prose is what
    produces that, and a picture of the wrong molecule is worse than an error. That check, and the
    whitespace one beside it, are `require_whole_string`'s and run in `render_svg` before this is
    called, because they have to hold for the whole reaction rather than per component.
    """
    try:
        reaction = rdChemReactions.ReactionFromSmarts(smiles, useSmiles=True)
    except ValueError as exc:
        raise InvalidSmilesError(f"not a drawable reaction SMILES: {smiles!r}") from exc
    if reaction.GetNumReactantTemplates() + reaction.GetNumProductTemplates() == 0:
        raise InvalidSmilesError(f"a reaction with no reactants and no products: {smiles!r}")
    return reaction
