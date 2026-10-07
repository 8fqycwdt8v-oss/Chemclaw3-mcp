"""Draw a molecule or a reaction as an SVG — the most expensive thing this server does.

Coordinate generation and drawing hold the GIL, so `tools.py` runs this in a worker thread: that
protects the event loop and `/healthz` but buys no parallelism (see `engine/admission.py`).
"""

from __future__ import annotations

from collections.abc import Sequence

from mcp_server_kit.limits import echo, env_bound, smiles_length_error
from rdkit import Chem
from rdkit.Chem import Draw, rdChemReactions
from rdkit.Chem.Draw import rdMolDraw2D

from chemclaw_mcp_chem.engine.chem import (
    InvalidSmilesError,
    require_molecule,
    require_whole_string,
)

__all__ = ["MAX_DEPICTION_CHARS", "MINIMUM_RENDER_SIZE_PX", "RENDER_SIZE_PX", "render_svg"]

#: The smallest canvas RDKit can put an atom label on, in pixels — its own `minFontSize`. Below it
#: the label no longer fits and a depiction cannot say which molecule it is.
MINIMUM_RENDER_SIZE_PX = int(rdMolDraw2D.MolDrawOptions().minFontSize)

# Edge length of a rendered depiction, in pixels; a reaction is drawn twice as wide as it is tall.
# Configurable so a deployment with larger chat cards can change it without a code edit.
RENDER_SIZE_PX = env_bound(
    "CHEMCLAW_CHEM_RENDER_SIZE_PX",
    default=320,
    # The floor is the label minimum, not 1: a zero canvas returns an empty picture as a success,
    # and RDKit silently replaces a negative size with its own.
    minimum=MINIMUM_RENDER_SIZE_PX,
    consequence=(
        "a canvas that small cannot carry an atom label at any scale, so every depiction would "
        "come back as a well-formed SVG of nothing"
    ),
)

# The largest molecule (or whole reaction) this server will lay out and draw.
#
# `Compute2DCoords` is strongly superlinear and its worker thread cannot be cancelled, so this is
# checked before it runs. Far below this a depiction is already an unreadable tangle.
MAX_DEPICTION_ATOMS = env_bound(
    "CHEMCLAW_CHEM_MAX_DEPICTION_ATOMS",
    default=250,
    # One atom: the guard refuses anything *above* this, and every molecule that parses has at
    # least one, so `0` refuses the whole tool while the pod starts and reports itself ready.
    minimum=1,
    consequence="every molecule and every reaction would be refused before it is laid out",
)

# The largest SVG document this server will hand back, in characters.
#
# The atom bound limits what a depiction costs this pod, not what leaves it. Chemclaw3 cuts long
# tool results head-and-tail, which turns an oversized SVG into a broken fragment, so this refuses
# whole instead. Set above the largest drug substance's drawing and under Chemclaw3's single-result
# ceiling; highlighting roughly doubles the document. The floor is 1 because the smallest SVG
# depends on the molecule and canvas; it catches an operator typing `0` or a negative.
MAX_DEPICTION_CHARS = env_bound(
    "CHEMCLAW_CHEM_MAX_DEPICTION_CHARS",
    default=50000,
    minimum=1,
    consequence=(
        "every drawing would be refused after it had been rendered; the smallest document this "
        "server emits is about 1,200 characters, so a ceiling anywhere near the floor refuses "
        "everything too"
    ),
)


def render_svg(smiles: str, highlight_atoms: Sequence[int] | None = None) -> str:
    """An inline SVG document depicting `smiles`, which may be a molecule or a reaction.

    A reaction is anything containing `>>`; everything else goes through the strict parse, so a
    string RDKit would truncate is refused rather than drawn. `highlight_atoms` draws the chosen
    atoms and the bonds between them so a chemist can verify a choice at a glance; out-of-range
    indices are refused, since a silently missing highlight would look like confirmation.

    Raises:
        InvalidSmilesError: the string is not a drawable molecule or reaction, or an index does not
            address one of its atoms.
    """
    # The whole-string guard runs before the reaction branch so it covers reactions too; the length
    # bound runs first so a megastring never reaches the reaction parser.
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
    return _within_bound(str(drawer.GetDrawingText()), highlight_atoms)


def _within_bound(svg: str, highlight_atoms: Sequence[int] | None) -> str:
    """Return `svg` if it is inside `MAX_DEPICTION_CHARS`, and refuse it whole if it is not.

    Measured on the finished drawing, because highlights make the size unpredictable from the atom
    count and the render is already bounded. Never a prefix: a cut SVG renders nothing. The message
    names the caller's levers — drop the highlights, or draw less.
    """
    if len(svg) <= MAX_DEPICTION_CHARS:
        return svg
    lever = (
        "drawing it without the atom highlights (they roughly double the document), or "
        if highlight_atoms
        else ""
    )
    raise InvalidSmilesError(
        f"this depiction is {len(svg)} characters, above the {MAX_DEPICTION_CHARS}-character "
        "limit on what one drawing may return. It is refused whole rather than cut, because a "
        "truncated SVG renders nothing at all. Try "
        f"{lever}drawing a fragment of the molecule instead; raise "
        "CHEMCLAW_CHEM_MAX_DEPICTION_CHARS if this deployment's chat surface can carry it."
    )


def _refuse_if_too_large(num_atoms: int, subject: str) -> None:
    """Refuse a depiction above `MAX_DEPICTION_ATOMS`, *before* the coordinate embedding runs.

    A worded `InvalidSmilesError` (a `ValueError`), so it reaches the model verbatim.
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

    `DrawReaction` lays out every component, so the whole reaction is what the bound must price.
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

    `ReactionFromSmarts` raises a bare `ValueError` with RDKit's parser wording, which would reach
    the model verbatim, so it is translated; and it accepts the empty reaction `">>"`, which is
    refused here. Whitespace and non-ASCII checks run in `render_svg` over the whole reaction.
    """
    try:
        reaction = rdChemReactions.ReactionFromSmarts(smiles, useSmiles=True)
    except ValueError as exc:
        raise InvalidSmilesError(f"not a drawable reaction SMILES: {echo(smiles)!r}") from exc
    if reaction.GetNumReactantTemplates() + reaction.GetNumProductTemplates() == 0:
        raise InvalidSmilesError(f"a reaction with no reactants and no products: {echo(smiles)!r}")
    return reaction
