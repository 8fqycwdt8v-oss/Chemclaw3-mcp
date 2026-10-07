"""Which bonds of a molecule can be rotated, and how a chemist names one.

An atom-index pair names a different bond once the SMILES is rewritten, with no error anywhere, so
a torsion gets a **handle** derived from the molecule rather than atom order. The candidate set is
defined here, not by `CalcNumRotatableBonds`, which excludes terminal tops and amides — the bonds
barrier questions are usually about.
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

# What sort of bond this is, in a chemist's words: what a request in words is matched against.
TorsionKind = Literal[
    "amide", "ester", "biaryl", "conjugated", "benzylic", "ether", "amine", "alkyl", "top", "xh"
]

# Each kind's environment, in order: the first pattern whose two matched atoms are this bond's
# wins. `top` and `xh` are decided by topology, not a pattern.
_KINDS: tuple[tuple[TorsionKind, str], ...] = (
    ("amide", "[CX3](=[OX1])[NX3]"),
    ("ester", "[CX3](=[OX1])[OX2H0]"),
    ("biaryl", "[a]-[a]"),
    ("benzylic", "[a]-[CX4,NX3,OX2]"),
    # Two atoms, since `_matched_pairs` reads the first and last matched atom as the bond. The guard
    # keeps an ester's alkyl-oxygen bond out.
    ("ether", "[OX2;!$(O[CX3]=[OX1])][CX4]"),
    ("amine", "[CX4][NX3;!$(N[CX3]=[OX1])]"),
)


class Torsion(BaseModel):
    """One rotatable bond of a molecule, named so the name survives a rewritten SMILES."""

    torsion_id: str = Field(
        description="The handle for this torsion — stable across every way of writing the molecule."
    )
    atoms: list[int] = Field(
        description=(
            "The four atom indices defining the dihedral, or empty for a rotor whose rotating end "
            "carries only hydrogens (`top` and `xh`) — a dihedral through one of those needs a "
            "hydrogen index, which means something only inside one explicit-H numbering. Chosen "
            "canonically, so they are the same four atoms whichever way the molecule was written."
        )
    )
    bond: list[int] = Field(description="The two atom indices of the bond itself.")
    label: str = Field(description="What a chemist calls this bond.")
    kind: TorsionKind
    smarts: str = Field(
        description=(
            "The environment this bond was recognised by, so the label is checkable. Empty for a "
            "kind decided by topology rather than by a pattern — `alkyl`, `conjugated`, `top` and "
            "`xh`."
        )
    )
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


def torsion_handle(
    mol: Chem.Mol,
    bond: tuple[int, int],
    classes: list[int] | None = None,
    written: str | None = None,
) -> str:
    """A content-addressed name for one rotatable bond of `mol`.

    Stable under SMILES rewriting (atoms named by canonical symmetry class, `breakTies=False`),
    shared by symmetry-equivalent bonds (`tests/test_torsions.py` checks this against the
    automorphism group), and carrying the RDKit version so a handle fails loudly under a different
    build rather than resolving to a different bond.

    Args:
        mol: The molecule the bond belongs to.
        bond: The two atom indices of the bond, in either order.
        classes: The molecule's canonical symmetry classes, if already computed. Pass them when
            naming many bonds: the whole-molecule passes are the entire cost.
        written: The molecule's canonical SMILES, on the same terms.

    Returns:
        `tor_` followed by sixteen hex characters.
    """
    if classes is None:
        classes = list(Chem.CanonicalRankAtoms(mol, breakTies=False))
    if written is None:
        written = str(Chem.MolToSmiles(mol))
    low, high = sorted((classes[bond[0]], classes[bond[1]]))
    payload = f"{rdkit.__version__}|{written}|{low}-{high}"
    return "tor_" + hashlib.sha256(payload.encode()).hexdigest()[:16]


def enumerate_torsion_candidates(smiles: str) -> list[Torsion]:
    """Every rotatable bond of `smiles`, one entry per symmetry-distinct torsion.

    A candidate is an acyclic single bond between two heavy atoms, not to an sp atom. A bond whose
    one side carries only hydrogens has no dihedral atoms (a hydrogen index means nothing outside
    one numbering) but is still reported: a `top` (methyl, whose effect is in the quasi-RRHO low
    modes) or an `xh` rotor (O-H, S-H, N-H, whose barrier is not).

    Raises:
        InvalidSmilesError: `smiles` is not a molecule.
    """
    mol = require_molecule(smiles)
    ranks = list(Chem.CanonicalRankAtoms(mol, breakTies=True))
    classes = list(Chem.CanonicalRankAtoms(mol, breakTies=False))
    written = str(Chem.MolToSmiles(mol))
    matched = {kind: _matched_pairs(mol, pattern) for kind, pattern in _KINDS}

    by_handle: dict[str, list[tuple[int, int]]] = {}
    for chem_bond in mol.GetBonds():
        if not _is_candidate(mol, chem_bond):
            continue
        low, high = sorted((chem_bond.GetBeginAtomIdx(), chem_bond.GetEndAtomIdx()))
        handle = torsion_handle(mol, (low, high), classes, written)
        by_handle.setdefault(handle, []).append((low, high))

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
                # Empty when no pattern matched, rather than a match-everything pattern.
                smarts=dict(_KINDS).get(kind, ""),
                symmetry_order=(order := _symmetry_order(mol, bond, classes)),
                period_degrees=360.0 / order,
                equivalent_bonds=[list(pair) for pair in sorted(bonds)],
            )
        )
    # Deterministic order across runs and spellings; rotors with no dihedral last.
    return sorted(torsions, key=lambda torsion: (not torsion.atoms, torsion.bond))


def _is_candidate(mol: Chem.Mol, bond: Chem.Bond) -> bool:
    """Is this an acyclic single bond between two heavy atoms with something to rotate?

    A ring bond cannot be driven without deforming the ring, and a bond to an sp atom has no
    dihedral.
    """
    if bond.IsInRing() or bond.GetBondType() != Chem.BondType.SINGLE:
        return False
    begin, end = bond.GetBeginAtom(), bond.GetEndAtom()
    if any(atom.GetAtomicNum() == 1 for atom in (begin, end)):
        return False
    if any(_is_linear(atom) for atom in (begin, end)):
        return False
    # A monovalent end (C-Cl) has nothing off-axis to rotate. A hydroxyl does (its hydrogen), so the
    # test is any substituent, not a heavy one.
    return all(_has_a_substituent(atom, other) for atom, other in ((begin, end), (end, begin)))


def _has_a_substituent(atom: Chem.Atom, other: Chem.Atom) -> bool:
    """Does this end of the bond carry anything besides the bond itself, off the axis?"""
    return bool(_heavy_neighbours(atom, other.GetIdx())) or atom.GetTotalNumHs() > 0


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

    The outer two are each end's highest-canonically-ranked heavy neighbour, independent of
    spelling.
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

    The least common multiple of each end's axis order (`_end_order`): toluene's methyl against the
    ring gives 6, so a 60 degree scan covers it — far fewer constrained optimizations than 360.
    """
    return math.lcm(*(_end_order(mol, bond[side], bond[1 - side], classes) for side in (0, 1)))


def _end_order(mol: Chem.Mol, atom_index: int, other: int, classes: list[int]) -> int:
    """The order of the rotational axis one end of the bond has *about that bond*.

    The substituents must be equivalent (hydrogens counted via `GetTotalNumHs`) **and** fill every
    azimuthal position (`_fills_the_azimuth`). Graph equivalence alone is not symmetry: a pyramidal
    amine's lone pair takes the third slot, so it has no C2 axis, and Chemclaw3 scans only
    `[0, period)`, so overcounting would leave part of the profile uncomputed.
    """
    atom = mol.GetAtomWithIdx(atom_index)
    heavy = _heavy_neighbours(atom, other)
    hydrogens = atom.GetTotalNumHs()
    if heavy and hydrogens:
        return 1
    count = len(heavy) or hydrogens
    if count < 2 or len({classes[one.GetIdx()] for one in heavy}) > 1:
        return 1
    return count if _fills_the_azimuth(atom, count) else 1


def _fills_the_azimuth(atom: Chem.Atom, count: int) -> bool:
    """Do `count` equivalent substituents leave no other azimuthal position occupied?

    Only two geometries qualify: a tetrahedral centre with three (methyl, CF3, tert-butyl) and a
    trigonal-planar centre with two (aryl ortho carbons, nitro oxygens). A three-connection SP3
    centre fails (its lone pair), as does anything RDKit could not hybridise — over-scanning, the
    safe direction.
    """
    connections = atom.GetDegree() + atom.GetTotalNumHs()
    hybridisation = atom.GetHybridization()
    if hybridisation == Chem.HybridizationType.SP3:
        return count == 3 and connections == 4
    if hybridisation == Chem.HybridizationType.SP2 or atom.GetIsAromatic():
        return count == 2 and connections == 3
    return False


def _matched_pairs(mol: Chem.Mol, pattern: str) -> set[tuple[int, int]]:
    """The bonds this SMARTS matches, as sorted index pairs of its first and last matched atoms.

    Compiled per call: parsing is a small share of the call, and a shared cache would put the
    recursive patterns under `RDK_BUILD_THREADSAFE_SSS`.
    """
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
        return "top" if _is_symmetric_top(mol, bond) else "xh"
    for kind, _ in _KINDS:
        if bond in matched[kind]:
            return kind
    return "conjugated" if mol.GetBondBetweenAtoms(*bond).GetIsConjugated() else "alkyl"


def _is_symmetric_top(mol: Chem.Mol, bond: tuple[int, int]) -> bool:
    """Is the hydrogen-only end of this bond a *symmetric* top — three hydrogens, so a methyl?

    A methyl's barrier is already in the quasi-RRHO low modes; an O-H's, S-H's or N-H's is not.
    """
    rotating, _ = _rotating_end(mol, bond)
    return rotating.GetTotalNumHs() >= 3


def _rotating_end(mol: Chem.Mol, bond: tuple[int, int]) -> tuple[Chem.Atom, Chem.Atom]:
    """The end of a dihedral-less rotor that carries only hydrogens, and the atom it turns on."""
    begin, end = (mol.GetAtomWithIdx(index) for index in bond)
    if _heavy_neighbours(begin, end.GetIdx()):
        return end, begin
    return begin, end


def _label(mol: Chem.Mol, bond: tuple[int, int], kind: TorsionKind) -> str:
    """What to call this bond in a sentence a chemist can check the choice against."""
    begin, end = (mol.GetAtomWithIdx(index) for index in bond)
    if kind in ("top", "xh"):
        rotating, anchor = _rotating_end(mol, bond)
        if kind == "xh":
            return f"the {_symbol(rotating)}-H rotation on {_symbol(anchor)}{anchor.GetIdx()}"
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
