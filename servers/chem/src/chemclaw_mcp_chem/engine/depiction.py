"""Draw a molecule or a reaction as an SVG — the most expensive thing this server does.

2D-coordinate generation and rasterising to SVG are CPU-bound C++ that holds the GIL for tens of
milliseconds on a drug-sized molecule. Chemclaw3 measured the consequence of doing that on the
event loop: throughput flat from 10 to 50 concurrent users. So `tools.py` awaits this in a worker
thread — RDKit releases the GIL for the heavy passes, so the threads are real parallelism.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from rdkit import Chem
from rdkit.Chem import Draw, rdChemReactions
from rdkit.Chem.Draw import rdMolDraw2D

from chemclaw_mcp_chem.engine.chem import InvalidSmilesError, require_molecule

__all__ = ["RENDER_SIZE_PX", "render_svg"]

# Edge length of a rendered depiction, in pixels; a reaction is drawn twice as wide as it is tall.
#
# Chemclaw3 carries this as `settings.structure_render_size_px`, default 320, for a reason that
# survives the port: a deployment whose chat surface renders larger cards must be able to change it
# without a code edit. This server has no settings object of its own — one integer does not earn a
# pydantic-settings dependency — so the same knob is one environment variable, read once at import
# and prefixed like every other variable this fleet reads.
RENDER_SIZE_PX = int(os.environ.get("CHEMCLAW_CHEM_RENDER_SIZE_PX", "320"))


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
    if ">>" in smiles:
        if highlight_atoms:
            raise InvalidSmilesError("a reaction drawing takes no atom highlight")
        drawer = rdMolDraw2D.MolDraw2DSVG(RENDER_SIZE_PX * 2, RENDER_SIZE_PX)
        drawer.DrawReaction(_reaction(smiles))
    else:
        mol = require_molecule(smiles)
        # Compute 2D coordinates so the depiction is laid out, not collapsed on the origin.
        Draw.rdDepictor.Compute2DCoords(mol)
        drawer = rdMolDraw2D.MolDraw2DSVG(RENDER_SIZE_PX, RENDER_SIZE_PX)
        atoms = _checked_atoms(mol, highlight_atoms)
        drawer.DrawMolecule(mol, highlightAtoms=atoms, highlightBonds=_spanned_bonds(mol, atoms))
    drawer.FinishDrawing()
    return str(drawer.GetDrawingText())


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

    The non-ASCII check is the third case and the quiet one: RDKit skips a run of non-ASCII bytes at
    a component's edges, so `"°C>>CC=O"` parses — as *methane* reacting to acetaldehyde. Prose is
    what produces that, and a picture of the wrong molecule is worse than an error.
    """
    if not smiles.isascii():
        raise InvalidSmilesError(f"invalid reaction SMILES (non-ASCII characters): {smiles!r}")
    try:
        reaction = rdChemReactions.ReactionFromSmarts(smiles, useSmiles=True)
    except ValueError as exc:
        raise InvalidSmilesError(f"not a drawable reaction SMILES: {smiles!r}") from exc
    if reaction.GetNumReactantTemplates() + reaction.GetNumProductTemplates() == 0:
        raise InvalidSmilesError(f"a reaction with no reactants and no products: {smiles!r}")
    return reaction
