"""Naming a torsion: what stays the same when the molecule is rewritten, and what must not merge.

Every claim `engine/torsions.py` makes is checked here as a number or a literal rather than as
prose, because the failure this module exists to prevent is *silent* — a scan of the wrong bond
returns a well-formed profile, not an error, so nothing downstream can catch it.

Three groups:

- **Invariance.** The same bond of the same compound, written three ways, is one handle.
- **The candidate set.** The rotatable-bond descriptor omits exactly the bonds people ask barriers
  about, and this module does not.
- **Equivalence.** Symmetry-distinct means symmetry-distinct: the cheap key (RDKit's canonical
  symmetry classes) is checked against the expensive one (the automorphism group), so a molecule
  where the two disagree turns this red instead of quietly merging two different bonds.
"""

from __future__ import annotations

import hashlib

import pytest
import rdkit
from chemclaw_mcp_chem.engine.chem import InvalidSmilesError
from chemclaw_mcp_chem.engine.torsions import (
    Torsion,
    enumerate_torsion_candidates,
    torsion_handle,
)
from rdkit import Chem
from rdkit.Chem import rdMolDescriptors


def _by_kind(smiles: str, kind: str) -> Torsion:
    """The one torsion of `smiles` with this kind, failing loudly if there is not exactly one."""
    found = [torsion for torsion in enumerate_torsion_candidates(smiles) if torsion.kind == kind]
    assert len(found) == 1, f"{smiles}: expected one {kind} torsion, got {[t.label for t in found]}"
    return found[0]


# One compound, three ways of writing it — the corpus behind the measurement this module exists
# for. Module-level rather than a class attribute so it is one list, not one per instance.
ACETANILIDE = ("CC(=O)Nc1ccccc1", "O=C(C)Nc1ccccc1", "c1ccc(NC(C)=O)cc1")


class TestTheHandleSurvivesARewrittenSmiles:
    """The property the whole design rests on, and the measurement that motivated it."""

    def test_one_bond_written_three_ways_is_one_handle(self) -> None:
        """Otherwise a bond named in one turn is a different bond in the next."""
        handles = {_by_kind(smiles, "amide").torsion_id for smiles in ACETANILIDE}
        assert len(handles) == 1, f"the amide C-N got {len(handles)} handles: {sorted(handles)}"

    def test_the_indices_it_replaces_do_not_survive(self) -> None:
        """The measurement this module exists for: same compound, same bond, different integers.

        And worse than different — *valid*. The indices that name the amide C-N in one writing name
        a real aromatic ring bond in another, so a scan driven from them runs, converges, and
        answers a question nobody asked.
        """
        indices = {tuple(_by_kind(smiles, "amide").bond) for smiles in ACETANILIDE}
        assert len(indices) > 1, "the premise failed: these writings agree on the indices"

        amide_here = _by_kind("c1ccc(NC(C)=O)cc1", "amide").bond
        elsewhere = Chem.MolFromSmiles("CC(=O)Nc1ccccc1")
        same_integers = elsewhere.GetBondBetweenAtoms(*amide_here)
        assert same_integers is not None, "the premise failed: those integers are not a bond there"
        assert same_integers.IsInRing(), "the premise failed: that bond is not a ring bond"

    def test_a_different_bond_of_the_same_molecule_is_a_different_handle(self) -> None:
        """Invariance is worthless without distinctness: a merge of two bonds is the same defect."""
        torsions = enumerate_torsion_candidates("CC(=O)Nc1ccccc1")
        assert len({torsion.torsion_id for torsion in torsions}) == len(torsions)

    def test_the_handle_names_the_rdkit_build(self) -> None:
        """A canonical ranking is a function of the build, so a stale handle must fail loudly.

        Resolving to a *different* bond after a toolchain bump is the silent failure this module
        exists to remove — so the version goes into the payload and an old handle simply stops
        matching. Asserted by construction rather than by upgrading RDKit inside a test.
        """
        mol = Chem.MolFromSmiles("CC(=O)Nc1ccccc1")
        ranks = list(Chem.CanonicalRankAtoms(mol, breakTies=False))
        low, high = sorted((ranks[1], ranks[3]))
        payload = f"{rdkit.__version__}|{Chem.MolToSmiles(mol)}|{low}-{high}"
        expected = "tor_" + hashlib.sha256(payload.encode()).hexdigest()[:16]
        assert torsion_handle(mol, (1, 3)) == expected


class TestTheCandidateSetIsNotTheRotatableBondCount:
    """Why this is not a thin wrapper over `CalcNumRotatableBonds`, in numbers."""

    @pytest.mark.parametrize(
        ("smiles", "descriptor_says"),
        [("Cc1ccccc1", 0), ("Cc1ccc(C)cc1", 0), ("CC(C)(C)c1ccccc1", 0), ("CC(=O)Nc1ccccc1", 1)],
    )
    def test_the_descriptor_omits_what_a_barrier_question_is_about(
        self, smiles: str, descriptor_says: int
    ) -> None:
        """Pinned as literals: terminal tops and amides are excluded from that count by definition.

        Toluene reports zero rotatable bonds and has a methyl rotation; acetanilide reports one and
        that one is *not* the amide. Both are exactly the bond a chemist asking about a rotational
        barrier means.
        """
        mol = Chem.MolFromSmiles(smiles)
        assert rdMolDescriptors.CalcNumRotatableBonds(mol) == descriptor_says

    def test_a_top_is_listed_and_carries_no_dihedral(self) -> None:
        """It is a real rotation, so it is reported; its dihedral needs a hydrogen, so it is not."""
        top = _by_kind("Cc1ccccc1", "top")
        assert top.atoms == []
        assert top.bond == [0, 1]
        # Three hydrogens against the ring's two equivalent ortho carbons: a 60-degree period.
        assert (top.symmetry_order, top.period_degrees) == (6, 60.0)

    def test_the_amide_and_the_tert_butyl_axis_are_both_candidates(self) -> None:
        """The two the descriptor drops are the two this must not."""
        assert _by_kind("CC(=O)Nc1ccccc1", "amide").atoms == [2, 1, 3, 4]
        assert _by_kind("CC(C)(C)c1ccccc1", "benzylic").bond == [1, 4]

    def test_a_ring_bond_is_never_a_candidate(self) -> None:
        """Driving one is a ring pucker, not a rotation — the same rule bond cleavage skips on."""
        mol = Chem.MolFromSmiles("C1CCCCC1")
        assert enumerate_torsion_candidates("C1CCCCC1") == []
        assert mol.GetNumBonds() == 6, "the premise failed: cyclohexane has six bonds to skip"

    def test_a_linear_axis_is_never_a_candidate(self) -> None:
        """There is no dihedral about a triple bond: the three atoms are collinear."""
        assert enumerate_torsion_candidates("CC#CC") == []

    def test_an_unparseable_smiles_is_refused(self) -> None:
        """The same strict parse the rest of this server uses: a truncation cannot get through."""
        with pytest.raises(InvalidSmilesError):
            enumerate_torsion_candidates("CCO junk")


class TestSymmetry:
    """The two things the symmetry order is for: a shorter scan, and a correct population."""

    @pytest.mark.parametrize(
        ("smiles", "kind", "order", "period"),
        [
            ("c1ccc(-c2ccccc2)cc1", "biaryl", 2, 180.0),
            ("CN(C)C=O", "amide", 2, 180.0),
            ("CC(=O)Nc1ccccc1", "amide", 1, 360.0),
            ("CCCC", "alkyl", 1, 360.0),
            ("Cc1ccccc1", "top", 6, 60.0),
        ],
    )
    def test_the_period_is_the_range_a_scan_has_to_cover(
        self, smiles: str, kind: str, order: int, period: float
    ) -> None:
        """Biphenyl repeats every 180 degrees and DMF every 180; acetanilide does not repeat.

        This is not cosmetic. Every degree not scanned is a constrained optimization not run, and
        every one of those is a real calculation.
        """
        torsion = _by_kind(smiles, kind)
        assert (torsion.symmetry_order, torsion.period_degrees) == (order, period)

    def test_equivalent_copies_are_one_entry(self) -> None:
        """p-xylene has two methyls and one question about them."""
        top = _by_kind("Cc1ccc(C)cc1", "top")
        assert top.equivalent_bonds == [[0, 1], [4, 5]]

    @pytest.mark.parametrize(
        "smiles",
        [
            "CC(=O)Nc1ccccc1",
            "Cc1ccc(C)cc1",
            "CCCC",
            "CC(C)(C)c1ccccc1",
            "c1ccc2ccccc2c1",
            "CC(=O)OCC",
            "OCCO",
            "c1ccc(-c2ccccc2)cc1",
            "Cc1ccccc1-c1ccccc1C",
            "CC(C)CC(C)C",
            "CN(C)C=O",
            "O=C(O)c1ccccc1O",
            "CCOC(=O)c1ccc(N)cc1",
            "CC(C)(C)OC(=O)N1CCCCC1",
            "c1ccc(COc2ccccc2)cc1",
            "FC(F)(F)c1ccccc1",
            "CCN(CC)CC",
            "CSc1ccccc1",
            "N#Cc1ccccc1C#N",
            "c1ccc(Nc2ccccc2)cc1",
            "CC(=O)c1ccc(C(C)=O)cc1",
        ],
    )
    def test_the_cheap_equivalence_agrees_with_the_expensive_one(self, smiles: str) -> None:
        """Symmetry classes group bonds the way the automorphism group does — checked, not assumed.

        The handle merges two bonds when their canonical *symmetry classes* match, which is cheap
        and writing-invariant. Vertex orbits do not determine edge orbits in general, so this
        compares the grouping against the real thing: the bond orbits under the molecule's own
        automorphisms. A false merge would mean two chemically different bonds sharing a handle,
        and a scan of one being reported as the other.
        """
        mol = Chem.MolFromSmiles(smiles)
        orbit = _bond_orbits(mol)
        merged: dict[str, set[int]] = {}
        for bond in mol.GetBonds():
            pair = (bond.GetBeginAtomIdx(), bond.GetEndAtomIdx())
            merged.setdefault(torsion_handle(mol, pair), set()).add(orbit[tuple(sorted(pair))])
        for handle, orbits in merged.items():
            assert len(orbits) == 1, f"{smiles}: {handle} merges {len(orbits)} automorphism orbits"


def _bond_orbits(mol: Chem.Mol) -> dict[tuple[int, int], int]:
    """Each bond mapped to its orbit under the molecule's automorphisms, by union-find.

    A self-substructure-match with `uniquify=False` enumerates the automorphism group; a bond and
    its image under any of them are the same bond up to symmetry.
    """
    parent: dict[tuple[int, int], tuple[int, int]] = {}

    def find(item: tuple[int, int]) -> tuple[int, int]:
        while parent.setdefault(item, item) != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    bonds = [tuple(sorted((b.GetBeginAtomIdx(), b.GetEndAtomIdx()))) for b in mol.GetBonds()]
    for bond in bonds:
        find(bond)
    for mapping in mol.GetSubstructMatches(mol, uniquify=False, useChirality=True, maxMatches=5000):
        for begin, end in bonds:
            image = tuple(sorted((mapping[begin], mapping[end])))
            first, second = find((begin, end)), find(image)
            if first != second:
                parent[first] = second
    return {bond: hash(find(bond)) for bond in bonds}
