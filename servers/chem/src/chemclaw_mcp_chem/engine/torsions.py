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
    "amide", "ester", "biaryl", "conjugated", "benzylic", "ether", "amine", "alkyl", "top", "xh"
]

# The environment each kind is recognised by, in order: the first pattern whose two matched atoms
# are this bond's two atoms wins. `top` and `xh` are not here — both are decided by the bond's own
# topology (one side carries no heavy neighbour), not by a substructure.
_KINDS: tuple[tuple[TorsionKind, str], ...] = (
    ("amide", "[CX3](=[OX1])[NX3]"),
    ("ester", "[CX3](=[OX1])[OX2H0]"),
    ("biaryl", "[a]-[a]"),
    ("benzylic", "[a]-[CX4,NX3,OX2]"),
    # Two atoms, not three. `[CX4][OX2][CX4]` matched the two *carbons* — `_matched_pairs` reads
    # the first and last matched atom, and those are not bonded to each other — so this kind could
    # never be assigned and every ether bond came back `alkyl`. The guard keeps an ester's
    # alkyl-oxygen bond out, since that is an ester rather than an ether.
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
        classes: The molecule's canonical symmetry classes, if the caller already has them.
            Omitted, they are computed here — and computing them once per *bond* is what made a
            600-atom molecule 18 s of CPU in a worker thread nothing can cancel.
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

    **A candidate is an acyclic single bond between two heavy atoms**, and nothing else — no
    druglikeness filter, no amide exclusion. A ring bond is not one: driving it is a ring pucker
    rather than a rotation, and it is left out for the same reason `enumerate_bond_cleavages` skips
    ring bonds. A triple bond and its neighbours are not one either: rotation about a linear axis
    has no dihedral.

    A bond whose one side carries only hydrogens is reported with **no** dihedral atoms, because a
    dihedral through it needs a hydrogen index and one of those means something only inside one
    explicit-H numbering. It is still a real rotation with a real barrier, and reporting it is the
    point: the descriptor everyone reaches for says toluene has zero rotatable bonds.

    **Two different things live in that bucket and they are not reported as one.** A rotating end
    carrying three hydrogens is a symmetric `top` — a methyl — whose energetic effect really is
    carried by the quasi-RRHO free-rotor treatment of the low modes. A rotating end carrying one or
    two is an `xh` rotor: an O-H, S-H or N-H. Acetamide's amide N-H (16-18 kcal/mol) and acetic
    acid's syn/anti O-H (5-6 kcal/mol, two genuinely distinct rotamers) are not in the low modes,
    and reporting them as tops told the model their barriers were already accounted for.

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
                # Empty when no pattern matched. It used to report `[*]-[*]`, which matches
                # everything and so is not the environment this bond was recognised by — a
                # checkable claim replaced by an unfalsifiable one.
                smarts=dict(_KINDS).get(kind, ""),
                symmetry_order=(order := _symmetry_order(mol, bond, classes)),
                period_degrees=360.0 / order,
                equivalent_bonds=[list(pair) for pair in sorted(bonds)],
            )
        )
    # Sorted so two runs, and two writings, list the same torsions in the same order, with the
    # rotors that carry no dihedral last — `top` and `xh` alike, which is what the sort said back
    # when those were one kind.
    return sorted(torsions, key=lambda torsion: (not torsion.atoms, torsion.bond))


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
    if any(_is_linear(atom) for atom in (begin, end)):
        return False
    # **A monovalent end has nothing to rotate.** A chlorine is one atom on the axis, so turning
    # about C-Cl moves nothing and there is no torsion — but the bond is acyclic, single and
    # between two heavy atoms, so every other rule here accepts it and `CCCl` listed "the Cl top
    # on C1". A hydroxyl is the opposite case and must stay: its hydrogen is off-axis, so O-H is a
    # real rotation, which is why the test is "any substituent at all" rather than "a heavy one".
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

    Each end contributes the order of the axis it has about this bond — a methyl is 3-fold, a
    phenyl 2-fold, a pyramidal tertiary amine 1-fold whatever its substituents are (see
    `_end_order`). The profile's period is set by both ends together, so the orders combine as a
    least common multiple — toluene's methyl against the ring's two equivalent ortho carbons gives
    6, and a 60 degree scan covers it.

    Worth the arithmetic rather than always scanning 360 degrees: for a symmetric top or a
    biaryl this is the difference between twelve constrained optimizations and two, and every one
    of them is a real calculation.
    """
    return math.lcm(*(_end_order(mol, bond[side], bond[1 - side], classes) for side in (0, 1)))


def _end_order(mol: Chem.Mol, atom_index: int, other: int, classes: list[int]) -> int:
    """The order of the rotational axis one end of the bond has *about that bond*.

    Two conditions, and the second is the one that is easy to miss. The substituents must be
    equivalent — hydrogens counted through `GetTotalNumHs` rather than as neighbours, because they
    are implicit here and they are the whole of a methyl's 3-fold symmetry — **and** they must
    exhaust the positions around the axis, which is what `_fills_the_azimuth` decides.

    **Equivalent is not the same as symmetric, and treating it as such was a wrong period rather
    than an untidy one.** RDKit's canonical symmetry classes are a *graph* equivalence. On a
    pyramidal three-coordinate centre — an aliphatic tertiary amine, a phosphine — the lone pair
    occupies the third azimuthal slot, so two constitutionally identical substituents sit near 120
    and 240 degrees apart and there is no C2 axis to rotate about. Measured on an MMFF-optimised
    dimethylethylamine, the two N-methyls sit 238 degrees apart, and a relaxed scan of the C-N bond
    puts V(phi) and V(phi+180) 5.8 kcal/mol apart on a 5.9 kcal/mol barrier. Chemclaw3 scans exactly
    `[0, period)` and weights the rotamer populations by `symmetry_order`, so crediting that axis
    left half the profile uncomputed and averaged over the half that was.
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

    An axis exists only where the equivalent substituents are *all* there is to place around the
    bond. Two geometries qualify and nothing else does:

    - a tetrahedral centre carrying three of them, the fourth position being the bond itself
      (methyl, CF3, tert-butyl, trimethylsilyl, a quaternary ammonium's three N-methyls);
    - a trigonal-planar centre carrying two, the third position being the bond (an aromatic ring's
      two ortho carbons, a planar amide's two N-methyls, a nitro group's two oxygens).

    A three-connection SP3 centre fails both, and that is the whole correction: its lone pair is in
    the position the symmetry would have to use. So is anything RDKit could not hybridise, which
    over-scans rather than under-scans — the direction that costs calculations instead of answers.
    """
    connections = atom.GetDegree() + atom.GetTotalNumHs()
    hybridisation = atom.GetHybridization()
    if hybridisation == Chem.HybridizationType.SP3:
        return count == 3 and connections == 4
    if hybridisation == Chem.HybridizationType.SP2 or atom.GetIsAromatic():
        return count == 2 and connections == 3
    return False


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
        return "top" if _is_symmetric_top(mol, bond) else "xh"
    for kind, _ in _KINDS:
        if bond in matched[kind]:
            return kind
    return "conjugated" if mol.GetBondBetweenAtoms(*bond).GetIsConjugated() else "alkyl"


def _is_symmetric_top(mol: Chem.Mol, bond: tuple[int, int]) -> bool:
    """Is the hydrogen-only end of this bond a *symmetric* top — three hydrogens, so a methyl?

    The distinction this makes is the one thing said about a dihedral-less rotor that a caller acts
    on: a methyl's barrier is already in the quasi-RRHO free-rotor treatment of the low modes, and
    an O-H's, S-H's or N-H's is not. Grouping them cost the amide-rotation question, which is the
    most-asked rotational barrier there is.
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
