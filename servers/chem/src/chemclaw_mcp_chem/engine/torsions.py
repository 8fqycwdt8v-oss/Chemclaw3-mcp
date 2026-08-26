"""Which bonds of a molecule can be rotated, and how a chemist names one.

**The problem this exists to remove.** Chemclaw3's `scan_coordinate` names an internal coordinate by
atom index, and its scan composer checks only that the indices are in range — a bounds check, not an
identity check. Measured on RDKit: `(4, 5)` is the amide C-N of `c1ccc(NC(C)=O)cc1` and an *aromatic
ring bond* of `CC(=O)Nc1ccccc1`, the same compound rewritten, really bonded, no error anywhere. A
mis-indexed torsion therefore returns a well-formed profile and a plausible barrier for a question
nobody asked. No chemist has those indices, which in practice means the model supplies them.

So a torsion needs a name that survives being written down: a **handle** derived from the molecule
rather than from the order its atoms happen to appear in.

**Why this is not a wrapper over the rotatable-bond count.** `CalcNumRotatableBonds` is a
druglikeness descriptor, and measured against RDKit it reports **0** for toluene, p-xylene and
tert-butylbenzene and **1** for acetanilide — the one it excludes there being the amide C-N, which
is the bond an anilide barrier question is about. It omits terminal tops (`!D1`) and amides by
definition, which is to say both classes of bond people ask barriers about. This module defines its
own candidate set instead.
"""

from __future__ import annotations

import hashlib
import math
from typing import Literal

import rdkit
from pydantic import BaseModel, Field
from rdkit import Chem

from chemclaw_mcp_chem.engine.chem import require_molecule

__all__ = ["Torsion", "TorsionKind", "enumerate_torsion_candidates", "torsion_handle"]

# What sort of bond this is, in the words a chemist uses for it. The classification decides nothing
# — it is what a request in words ("the amide bond", "the biaryl axis") is matched against, and what
# a reader checks the choice by.
TorsionKind = Literal[
    "amide", "ester", "biaryl", "conjugated", "benzylic", "ether", "amine", "alkyl", "top"
]

# The environment each kind is recognised by, in order: the first pattern whose two matched atoms
# are this bond's two atoms wins. `top` is not here — it is decided by the bond's own topology
# (one side carries no heavy neighbour), not by a substructure.
_KINDS: tuple[tuple[TorsionKind, str], ...] = (
    ("amide", "[CX3](=[OX1])[NX3]"),
    ("ester", "[CX3](=[OX1])[OX2H0]"),
    ("biaryl", "[a]-[a]"),
    ("benzylic", "[a]-[CX4,NX3,OX2]"),
    ("ether", "[CX4][OX2][CX4]"),
    ("amine", "[CX4][NX3;!$(N[CX3]=[OX1])]"),
)


class Torsion(BaseModel):
    """One rotatable bond of a molecule, named so the name survives a rewritten SMILES."""

    torsion_id: str = Field(
        description="The handle for this torsion — stable across every way of writing the molecule."
    )
    atoms: list[int] = Field(
        description=(
            "The four atom indices defining the dihedral, or empty for a symmetric top whose only "
            "substituents are hydrogens. Chosen canonically, so they are the same four atoms "
            "whichever way the molecule was written."
        )
    )
    bond: list[int] = Field(description="The two atom indices of the bond itself.")
    label: str = Field(description="What a chemist calls this bond.")
    kind: TorsionKind
    smarts: str = Field(description="The environment that was matched, so the label is checkable.")
    symmetry_order: int = Field(
        ge=1, description="How many times the profile repeats in a full 360 degree rotation."
    )
    period_degrees: float = Field(
        gt=0, description="360 / symmetry_order — the range a scan actually has to cover."
    )
    equivalent_bonds: list[list[int]] = Field(
        description=(
            "Every bond in this molecule that is this same torsion by symmetry, including this "
            "one. Scanning one of them answers for all of them."
        )
    )


def torsion_handle(mol: Chem.Mol, bond: tuple[int, int]) -> str:
    """A content-addressed name for one rotatable bond of `mol`.

    Three properties, and each is a defect it prevents:

    - **It does not change when the SMILES is rewritten.** The two atoms are named by their
      canonical symmetry class rather than by their index, so `CC(=O)Nc1ccccc1`,
      `O=C(C)Nc1ccccc1` and `c1ccc(NC(C)=O)cc1` all give the amide C-N one handle while the indices
      differ. That is the whole point: an index carried from one turn to the next silently becomes
      a different bond.
    - **Symmetry-equivalent bonds share it.** p-xylene's two methyls are one torsion, asked once.
      The class pair is RDKit's own symmetry classes (`breakTies=False`), which is a cheaper
      equivalence than enumerating the automorphism group; `tests/test_torsions.py` checks the two
      agree rather than assuming it, over a molecule set chosen to include the fused, symmetric and
      polysubstituted cases where they might not.
    - **It fails loudly after a toolchain bump.** The RDKit version is in the payload, because the
      canonical ranking is a function of that build. A handle minted under one build must *not*
      resolve under another — resolving to a different bond is the silent failure this whole module
      exists to remove, and that is `D-2026-08-16`'s `calc_version` lesson one level down.

    Args:
        mol: The molecule the bond belongs to.
        bond: The two atom indices of the bond, in either order.

    Returns:
        `tor_` followed by sixteen hex characters.
    """
    ranks = list(Chem.CanonicalRankAtoms(mol, breakTies=False))
    low, high = sorted((ranks[bond[0]], ranks[bond[1]]))
    payload = f"{rdkit.__version__}|{Chem.MolToSmiles(mol)}|{low}-{high}"
    return "tor_" + hashlib.sha256(payload.encode()).hexdigest()[:16]


def enumerate_torsion_candidates(smiles: str) -> list[Torsion]:
    """Every rotatable bond of `smiles`, one entry per symmetry-distinct torsion.

    **A candidate is an acyclic single bond between two heavy atoms**, and nothing else — no
    druglikeness filter, no amide exclusion. A ring bond is not one: driving it is a ring pucker
    rather than a rotation, and it is left out for the same reason `enumerate_bond_cleavages` skips
    ring bonds. A triple bond and its neighbours are not one either: rotation about a linear axis
    has no dihedral.

    A bond whose one side carries only hydrogens — a methyl or tert-butyl **top** — is reported with
    `kind="top"` and **no** dihedral atoms. It is a real rotation with a real barrier, and reporting
    it is the point: the descriptor everyone reaches for says toluene has zero rotatable bonds. But
    its dihedral needs a hydrogen, which exists only in a 3D structure with explicit hydrogens
    added, and a hydrogen index is exactly the kind of thing this module refuses to hand out. Its
    energetic effect is already carried by the quasi-RRHO free-rotor treatment of the low modes.

    Raises:
        InvalidSmilesError: `smiles` is not a molecule.
    """
    mol = require_molecule(smiles)
    ranks = list(Chem.CanonicalRankAtoms(mol, breakTies=True))
    classes = list(Chem.CanonicalRankAtoms(mol, breakTies=False))
    matched = {kind: _matched_pairs(mol, pattern) for kind, pattern in _KINDS}

    by_handle: dict[str, list[tuple[int, int]]] = {}
    for chem_bond in mol.GetBonds():
        if not _is_candidate(mol, chem_bond):
            continue
        low, high = sorted((chem_bond.GetBeginAtomIdx(), chem_bond.GetEndAtomIdx()))
        by_handle.setdefault(torsion_handle(mol, (low, high)), []).append((low, high))

    torsions: list[Torsion] = []
    for handle, bonds in by_handle.items():
        # The representative is the bond whose canonical ranks are lowest, so which member of an
        # equivalence class is described does not depend on how the molecule was written.
        bond = min(bonds, key=lambda pair: sorted((ranks[pair[0]], ranks[pair[1]])))
        dihedral = _dihedral(mol, bond, ranks)
        kind = _classify(mol, bond, matched, dihedral)
        torsions.append(
            Torsion(
                torsion_id=handle,
                atoms=list(dihedral),
                bond=list(bond),
                label=_label(mol, bond, kind),
                kind=kind,
                smarts=dict(_KINDS).get(kind, "[*]-[*]"),
                symmetry_order=(order := _symmetry_order(mol, bond, classes)),
                period_degrees=360.0 / order,
                equivalent_bonds=[list(pair) for pair in sorted(bonds)],
            )
        )
    # Sorted so two runs, and two writings, list the same torsions in the same order.
    return sorted(torsions, key=lambda torsion: (torsion.kind == "top", torsion.bond))


def _is_candidate(mol: Chem.Mol, bond: Chem.Bond) -> bool:
    """Is this an acyclic single bond between two heavy atoms with something to rotate?

    The two exclusions are geometric rather than stylistic. A bond in a ring cannot be driven
    without deforming the ring, and a bond to an sp-hybridised atom has no dihedral to drive — the
    three atoms are collinear, and RDKit's own rotatable-bond pattern excludes it for the same
    reason.
    """
    if bond.IsInRing() or bond.GetBondType() != Chem.BondType.SINGLE:
        return False
    begin, end = bond.GetBeginAtom(), bond.GetEndAtom()
    if any(atom.GetAtomicNum() == 1 for atom in (begin, end)):
        return False
    return not any(_is_linear(atom) for atom in (begin, end))


def _is_linear(atom: Chem.Atom) -> bool:
    """Does this atom sit on a linear axis, so that a dihedral through it is undefined?"""
    return atom.GetHybridization() == Chem.HybridizationType.SP or any(
        bond.GetBondType() == Chem.BondType.TRIPLE for bond in atom.GetBonds()
    )


def _heavy_neighbours(atom: Chem.Atom, exclude: int) -> list[Chem.Atom]:
    """The atom's heavy neighbours other than `exclude`, which is the other end of the bond."""
    return [
        neighbour
        for neighbour in atom.GetNeighbors()
        if neighbour.GetIdx() != exclude and neighbour.GetAtomicNum() > 1
    ]


def _dihedral(mol: Chem.Mol, bond: tuple[int, int], ranks: list[int]) -> tuple[int, ...]:
    """The four atoms defining this bond's dihedral, or `()` for a top.

    The outer two are each the *highest-canonically-ranked* heavy neighbour of their end. Ranked
    rather than lowest-index, because an index depends on how the molecule was written and the
    whole point of this module is a choice that does not.
    """
    begin, end = mol.GetAtomWithIdx(bond[0]), mol.GetAtomWithIdx(bond[1])
    first = _heavy_neighbours(begin, end.GetIdx())
    last = _heavy_neighbours(end, begin.GetIdx())
    if not first or not last:
        return ()
    return (
        max(first, key=lambda atom: ranks[atom.GetIdx()]).GetIdx(),
        begin.GetIdx(),
        end.GetIdx(),
        max(last, key=lambda atom: ranks[atom.GetIdx()]).GetIdx(),
    )


def _symmetry_order(mol: Chem.Mol, bond: tuple[int, int], classes: list[int]) -> int:
    """How many times the torsion profile repeats in a full turn.

    Each end contributes the number of *equivalent* substituents it carries, counting hydrogens: a
    methyl is 3-fold, a phenyl is 2-fold about its own axis, an unsymmetrical end is 1-fold. The
    profile's period is set by both ends together, so the orders combine as a least common
    multiple — toluene's methyl against the ring's two equivalent ortho carbons gives 6, and a
    60 degree scan covers it.

    Worth the arithmetic rather than always scanning 360 degrees: for a symmetric top or a
    biaryl this is the difference between twelve constrained optimizations and two, and every one
    of them is a real calculation.
    """
    return math.lcm(*(_end_order(mol, bond[side], bond[1 - side], classes) for side in (0, 1)))


def _end_order(mol: Chem.Mol, atom_index: int, other: int, classes: list[int]) -> int:
    """How many equivalent substituents one end of the bond carries.

    Hydrogens are counted through `GetTotalNumHs` rather than as neighbours, because they are
    implicit here — and they are the whole of a methyl's 3-fold symmetry. A substituent set that is
    not all one symmetry class has no rotational symmetry at all, which is the common case.
    """
    atom = mol.GetAtomWithIdx(atom_index)
    heavy = _heavy_neighbours(atom, other)
    hydrogens = atom.GetTotalNumHs()
    if heavy and hydrogens:
        return 1
    if hydrogens:
        return hydrogens
    return len(heavy) if len({classes[one.GetIdx()] for one in heavy}) == 1 else 1


def _matched_pairs(mol: Chem.Mol, pattern: str) -> set[tuple[int, int]]:
    """The bonds this SMARTS matches, as sorted index pairs of its first and last matched atoms."""
    # First and last matched atom, because that is where every pattern here puts the bond that
    # rotates: `[CX3](=[OX1])[NX3]` matches (C, O, N) and the amide bond is C-N, not C=O.
    query = Chem.MolFromSmarts(pattern)
    return {tuple(sorted((match[0], match[-1]))) for match in mol.GetSubstructMatches(query)}


def _classify(
    mol: Chem.Mol,
    bond: tuple[int, int],
    matched: dict[TorsionKind, set[tuple[int, int]]],
    dihedral: tuple[int, ...],
) -> TorsionKind:
    """Which kind of bond this is, in the order the patterns are written."""
    if not dihedral:
        return "top"
    for kind, _ in _KINDS:
        if bond in matched[kind]:
            return kind
    return "conjugated" if mol.GetBondBetweenAtoms(*bond).GetIsConjugated() else "alkyl"


def _label(mol: Chem.Mol, bond: tuple[int, int], kind: TorsionKind) -> str:
    """What to call this bond in a sentence a chemist can check the choice against."""
    begin, end = (mol.GetAtomWithIdx(index) for index in bond)
    if kind == "top":
        rotating = begin if not _heavy_neighbours(begin, end.GetIdx()) else end
        anchor = end if rotating.GetIdx() == begin.GetIdx() else begin
        return f"the {_group(rotating)} top on {_symbol(anchor)}{anchor.GetIdx()}"
    names = {
        "amide": "the amide",
        "ester": "the ester",
        "biaryl": "the biaryl axis",
        "benzylic": "the aryl",
        "ether": "the ether",
        "amine": "the amine",
        "conjugated": "the conjugated",
        "alkyl": "the",
    }
    return f"{names[kind]} {_symbol(begin)}{begin.GetIdx()}-{_symbol(end)}{end.GetIdx()} bond"


def _group(atom: Chem.Atom) -> str:
    """A rotating end's common name, for a top's label: methyl, tert-butyl, or its element."""
    if atom.GetSymbol() != "C":
        return atom.GetSymbol()
    return {3: "methyl", 2: "methylene", 1: "methine"}.get(atom.GetTotalNumHs(), "carbon")


def _symbol(atom: Chem.Atom) -> str:
    """The element symbol, lower-cased when aromatic, so a label shows what kind of atom it is."""
    return atom.GetSymbol().lower() if atom.GetIsAromatic() else atom.GetSymbol()
