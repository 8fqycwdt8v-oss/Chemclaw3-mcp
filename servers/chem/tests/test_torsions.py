"""Naming a torsion: what stays the same when the molecule is rewritten, and what must not merge.

A scan of the wrong bond returns a well-formed profile, so every claim is checked as a number or
literal:

- **Invariance.** The same bond, written three ways, is one handle.
- **The candidate set.** Bonds the rotatable-bond descriptor omits, but barriers are asked about,
  are included.
- **Equivalence.** The cheap key (canonical symmetry classes) is checked against the automorphism
  group, so a disagreement goes red instead of merging two different bonds.
"""

from __future__ import annotations

import hashlib
import time

import pytest
import rdkit
from chemclaw_mcp_chem.engine.chem import InvalidSmilesError
from chemclaw_mcp_chem.engine.torsions import (
    _KINDS,
    Torsion,
    enumerate_torsion_candidates,
    torsion_handle,
)
from rdkit import Chem
from rdkit.Chem import rdDistGeom, rdForceFieldHelpers, rdMolDescriptors, rdMolTransforms


def _by_kind(smiles: str, kind: str) -> Torsion:
    """The one torsion of `smiles` with this kind, failing loudly if there is not exactly one."""
    found = [torsion for torsion in enumerate_torsion_candidates(smiles) if torsion.kind == kind]
    assert len(found) == 1, f"{smiles}: expected one {kind} torsion, got {[t.label for t in found]}"
    return found[0]


# One compound, three ways of writing it — the corpus behind the measurement this module exists
# for. Module-level rather than a class attribute so it is one list, not one per instance.
ACETANILIDE = ("CC(=O)Nc1ccccc1", "O=C(C)Nc1ccccc1", "c1ccc(NC(C)=O)cc1")

# One compound per pattern in `_KINDS`, in its order. Module-level because two tests read it: the
# one that assigns each kind, and the one that checks this list still covers the table.
ONE_PER_PATTERN: tuple[tuple[str, str], ...] = (
    ("CC(=O)Nc1ccccc1", "amide"),
    ("CC(=O)OCC", "ester"),
    ("c1ccc(-c2ccccc2)cc1", "biaryl"),
    ("CC(C)(C)c1ccccc1", "benzylic"),
    ("CCOCC", "ether"),
    ("CCN(CC)CC", "amine"),
)

# What the pattern invariant is checked against: the representatives above plus compounds whose
# kinds are decided by topology, so a pattern is exercised where it is meant to fire and where it
# is not.
PATTERN_CORPUS: tuple[str, ...] = (
    *(smiles for smiles, _ in ONE_PER_PATTERN),
    "CC(C)CC(C)C",
    "C=CC=C",
    "Cc1ccccc1",
    "CCOC(=O)c1ccc(N)cc1",
    "c1ccc(COc2ccccc2)cc1",
    "CC(C)(C)OC(=O)N1CCCCC1",
    "COCCOC",
)


class TestTheHandleSurvivesARewrittenSmiles:
    """The property the whole design rests on, and the measurement that motivated it."""

    def test_one_bond_written_three_ways_is_one_handle(self) -> None:
        """Otherwise a bond named in one turn is a different bond in the next."""
        handles = {_by_kind(smiles, "amide").torsion_id for smiles in ACETANILIDE}
        assert len(handles) == 1, f"the amide C-N got {len(handles)} handles: {sorted(handles)}"

    def test_the_indices_it_replaces_do_not_survive(self) -> None:
        """The indices a handle replaces do not survive a rewrite, yet stay valid for another bond.

        So a scan driven from them would run and answer a question nobody asked.
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
        """The handle names the RDKit build, so a stale handle fails loudly rather than resolving
        elsewhere.
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
        """The rotatable-bond descriptor omits terminal tops and amides by definition.

        Toluene's methyl and acetanilide's amide are exactly what a barrier question means.
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

        Vertex orbits do not determine edge orbits in general, so the grouping is compared against
        bond orbits under the molecule's automorphisms.
        """
        mol = Chem.MolFromSmiles(smiles)
        orbit = _bond_orbits(mol)
        merged: dict[str, set[int]] = {}
        for bond in mol.GetBonds():
            begin, end = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
            ordered = (begin, end) if begin <= end else (end, begin)
            merged.setdefault(torsion_handle(mol, (begin, end)), set()).add(orbit[ordered])
        for handle, orbits in merged.items():
            assert len(orbits) == 1, f"{smiles}: {handle} merges {len(orbits)} automorphism orbits"


def _bond_orbits(mol: Chem.Mol) -> dict[tuple[int, int], int]:
    """Each bond mapped to its orbit under the molecule's automorphisms, by union-find.

    A self-substructure-match with `uniquify=False` enumerates the automorphism group; a bond and
    its image under any of them are the same bond up to symmetry.
    """
    parent: dict[tuple[int, int], tuple[int, int]] = {}

    def pair(one: int, other: int) -> tuple[int, int]:
        """A bond as an ordered pair — `tuple(sorted(...))` is a tuple of unknown length."""
        return (one, other) if one <= other else (other, one)

    def find(item: tuple[int, int]) -> tuple[int, int]:
        while parent.setdefault(item, item) != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    bonds = [pair(b.GetBeginAtomIdx(), b.GetEndAtomIdx()) for b in mol.GetBonds()]
    for bond in bonds:
        find(bond)
    for mapping in mol.GetSubstructMatches(mol, uniquify=False, useChirality=True, maxMatches=5000):
        for begin, end in bonds:
            image = pair(mapping[begin], mapping[end])
            first, second = find((begin, end)), find(image)
            if first != second:
                parent[first] = second
    return {bond: hash(find(bond)) for bond in bonds}


class TestTheKindIsACheckableClaim:
    """`kind` and `smarts` are what a human checks the choice by, so both have to be true.

    A kind that can never be assigned and a pattern that matches everything both rot silently.
    """

    @pytest.mark.parametrize(("smiles", "kind"), ONE_PER_PATTERN)
    def test_every_pattern_kind_can_actually_be_assigned(self, smiles: str, kind: str) -> None:
        """One representative per pattern in `_KINDS`, because a dead pattern is invisible.

        `_matched_pairs` reads the first and last matched atoms as the bond, so a pattern whose ends
        are not bonded can never match a bond.
        """
        assert _by_kind(smiles, kind).kind == kind

    def test_the_table_is_covered(self) -> None:
        """A pattern added without a representative is a pattern nobody has ever seen assigned."""
        assert {kind for _, kind in ONE_PER_PATTERN} == {kind for kind, _ in _KINDS}

    @pytest.mark.parametrize(
        ("smiles", "kind"),
        [("CC(C)CC(C)C", "alkyl"), ("C=CC=C", "conjugated"), ("Cc1ccccc1", "top")],
    )
    def test_a_kind_decided_by_topology_reports_no_pattern(self, smiles: str, kind: str) -> None:
        """A kind decided by topology reports an empty pattern, not `[*]-[*]`.

        A pattern matching every bond would be unfalsifiable in the field meant to make the label
        checkable.
        """
        assert _by_kind(smiles, kind).smarts == ""

    @pytest.mark.parametrize(("pattern_kind", "pattern"), _KINDS)
    def test_every_pattern_names_a_bond(self, pattern_kind: str, pattern: str) -> None:
        """Wherever a pattern matches, its first and last matched atoms are bonded to each other.

        The invariant `_matched_pairs` rests on, checked over every pattern and compound so a new
        three-atom pattern is red immediately.
        """
        query = Chem.MolFromSmarts(pattern)
        assert query is not None, f"{pattern_kind}: {pattern} is not a parseable SMARTS"
        for smiles in PATTERN_CORPUS:
            mol = Chem.MolFromSmiles(smiles)
            for match in mol.GetSubstructMatches(query):
                assert mol.GetBondBetweenAtoms(match[0], match[-1]) is not None, (
                    f"{pattern_kind}: {pattern} matched {match} of {smiles}, "
                    "whose first and last atoms are not bonded"
                )


class TestAMonovalentEndIsNotARotation:
    """Turning a bond whose far end is a single atom moves nothing, so it is not a candidate."""

    @pytest.mark.parametrize("smiles", ["CCCl", "ClCCCl", "FC(F)(F)c1ccccc1", "CCBr"])
    def test_a_terminal_halogen_is_not_a_torsion(self, smiles: str) -> None:
        """A terminal halogen is not a torsion: nothing rotates off that axis.

        A barrier scan about it would return a flat, meaningless profile.
        """
        halogens = {9, 17, 35, 53}
        mol = Chem.MolFromSmiles(smiles)
        for torsion in enumerate_torsion_candidates(smiles):
            atoms = [mol.GetAtomWithIdx(index) for index in torsion.bond]
            assert not any(atom.GetAtomicNum() in halogens for atom in atoms), (
                f"{smiles}: {torsion.label} rotates about a monovalent atom"
            )

    def test_a_hydroxyl_is_still_a_rotation(self) -> None:
        """The distinction the fix rests on: an O-H hydrogen is off the axis, a chlorine is on it.

        Which is why the rule is "carries any substituent" rather than "carries a heavy one" — the
        cheaper rule would have taken every alcohol's O-H rotation with the halides.
        """
        rotors = [t for t in enumerate_torsion_candidates("CCO") if not t.atoms]
        assert sorted(t.label for t in rotors) == [
            "the O-H rotation on C1",
            "the methyl top on C1",
        ]


# The step of the relaxed scan below, in degrees. Coarse on purpose: every point is a constrained
# minimization, and the deviation this test is looking for is the size of a whole barrier rather
# than a feature that needs resolving.
_SCAN_STEP_DEGREES = 30.0

# How far apart V(phi) and V(phi + period) may be before the claimed period is not a period, in
# kcal/mol. The controls below land at 0.0-1.0 and a pyramidal amine at 8.0, so the threshold is
# not what decides the result.
_PERIOD_TOLERANCE_KCAL = 1.5


def _relaxed_profile(smiles: str, atoms: list[int]) -> dict[float, float]:
    """A relaxed constrained MMFF scan of one dihedral, in kcal/mol relative to its own minimum.

    Independent of `symmetry_order`'s graph reasoning: the force field walks the real potential.
    Walked in both directions with geometry carried forward, taking the lower energy, since a
    relaxed scan is basin-local.
    """
    mol = Chem.AddHs(Chem.MolFromSmiles(smiles))
    rdDistGeom.EmbedMolecule(mol, randomSeed=0xC0FFEE)
    rdForceFieldHelpers.MMFFOptimizeMolecule(mol, maxIters=4000)
    points = int(360.0 / _SCAN_STEP_DEGREES)
    lowest: dict[float, float] = {}
    for direction in (1, -1):
        walk = Chem.Mol(mol)
        for step in range(points):
            angle = (direction * step * _SCAN_STEP_DEGREES) % 360.0
            first, second, third, fourth = atoms
            rdMolTransforms.SetDihedralDeg(walk.GetConformer(), first, second, third, fourth, angle)
            field = rdForceFieldHelpers.MMFFGetMoleculeForceField(
                walk, rdForceFieldHelpers.MMFFGetMoleculeProperties(walk)
            )
            field.MMFFAddTorsionConstraint(*atoms, False, angle - 0.05, angle + 0.05, 1.0e6)
            field.Minimize(maxIts=4000)
            energy = field.CalcEnergy()
            lowest[angle] = min(lowest.get(angle, energy), energy)
    floor = min(lowest.values())
    return {angle: energy - floor for angle, energy in lowest.items()}


def _period_deviation(profile: dict[float, float], period_degrees: float) -> float:
    """The largest |V(phi) - V(phi + period)| over the profile, in kcal/mol."""
    points = int(360.0 / _SCAN_STEP_DEGREES)
    shift = round(period_degrees / _SCAN_STEP_DEGREES)
    return max(
        abs(
            profile[step * _SCAN_STEP_DEGREES]
            - profile[((step + shift) % points) * _SCAN_STEP_DEGREES]
        )
        for step in range(points)
    )


class TestThePeriodIsARotationAndNotAGraphEquivalence:
    """A period is a claim about the potential, and it is checked against one.

    Graph-equivalent substituents on a pyramidal centre (tertiary amine, phosphine) are not related
    by a C2 axis, because the lone pair takes the third slot. Chemclaw3 scans `[0, period)` and
    weights by `symmetry_order`, so a false period would skip half of the profile.
    """

    @pytest.mark.parametrize(
        ("smiles", "kind", "order", "period"),
        [
            # The defect: two methyls on a pyramidal nitrogen are one symmetry class and are not
            # 180 degrees apart.
            ("CN(C)CC", "amine", 1, 360.0),
            ("CCN(CC)CC", "amine", 1, 360.0),
            ("CN(C)CCc1ccccc1", "amine", 1, 360.0),
            # A phosphine is the same pyramidal centre one row down. Triphenylphosphine is
            # unaffected, and that is the point: the P contributes 1, the phenyl end still
            # contributes its own C2, and lcm(2, 1) is 2.
            ("c1ccc(P(c2ccccc2)c2ccccc2)cc1", "alkyl", 2, 180.0),
            # Kept, and each for a reason the fix has to preserve: a planar (SP2) amide nitrogen
            # really is C2, an aromatic ring really is C2, and a methyl really is C3.
            ("CN(C)C=O", "amide", 2, 180.0),
            ("CC(=O)N(C)C", "amide", 2, 180.0),
            ("c1ccc(-c2ccccc2)cc1", "biaryl", 2, 180.0),
            ("CC(C)(C)c1ccccc1", "benzylic", 6, 60.0),
            ("Cc1ccccc1", "top", 6, 60.0),
        ],
    )
    def test_only_a_rotation_about_the_axis_shortens_the_scan(
        self, smiles: str, kind: str, order: int, period: float
    ) -> None:
        """Pinned per molecule, because each one is a different reason to credit an axis or not."""
        torsion = _by_kind(smiles, kind)
        assert (torsion.symmetry_order, torsion.period_degrees) == (order, period), (
            f"{smiles}: {torsion.label} claims order {torsion.symmetry_order} "
            f"(period {torsion.period_degrees}), expected {order} (period {period})"
        )

    @pytest.mark.parametrize(
        "smiles",
        [
            "c1ccc(-c2ccccc2)cc1",  # a real C2 on both ends: the control that says this measures
            "O=[N+]([O-])c1ccccc1",  # a real C2 from the ring
            "C=Cc1ccccc1",  # a real C2 from both
            "CCCC",  # no symmetry at all
            "CCOCC",
            "CN(C)CC",  # the defect: claimed 180, and V(phi) - V(phi+180) is the whole barrier
            "CCN(CC)CC",
        ],
    )
    def test_the_claimed_period_survives_a_relaxed_force_field_scan(self, smiles: str) -> None:
        """The claimed period survives a relaxed MMFF scan.

        Rotors whose neighbours must re-orient with them (e.g. tert-butyl) cannot be measured this
        way and are covered by the pinned expectations above.
        """
        for torsion in enumerate_torsion_candidates(smiles):
            if not torsion.atoms:
                continue
            profile = _relaxed_profile(smiles, torsion.atoms)
            deviation = _period_deviation(profile, torsion.period_degrees)
            barrier = max(profile.values())
            assert deviation <= _PERIOD_TOLERANCE_KCAL, (
                f"in: {smiles}  out: {torsion.label} period={torsion.period_degrees} deg — "
                f"the relaxed MMFF profile differs by {deviation:.2f} kcal/mol between phi and "
                f"phi+period on a {barrier:.2f} kcal/mol barrier, so it does not repeat there"
            )


class TestTheCanonicalViewIsComputedOncePerCall:
    """`torsion_handle` computes the canonical view once per call, not per candidate bond.

    Re-canonicalising per bond is quadratic in the atom count.
    """

    def test_a_six_hundred_atom_molecule_is_still_a_graph_operation(self) -> None:
        smiles = "C" * 600
        started = time.perf_counter()
        found = enumerate_torsion_candidates(smiles)
        elapsed = time.perf_counter() - started
        assert found
        assert elapsed < 3.0, (
            f"in: a {len(smiles)}-atom alkane  out: {len(found)} torsions in {elapsed:.2f} s"
        )

    def test_the_hoisted_handle_is_the_one_shot_handle(self) -> None:
        """A handle is carried between turns, so it must not depend on how it was computed."""
        for smiles in ("CC(=O)Nc1ccccc1", "CCOCC", "Cc1ccc(C)cc1"):
            mol = Chem.MolFromSmiles(smiles)
            for torsion in enumerate_torsion_candidates(smiles):
                bond = (torsion.bond[0], torsion.bond[1])
                assert torsion.torsion_id == torsion_handle(mol, bond), (
                    f"in: {smiles}  out: {torsion.label} has two names"
                )


class TestAnXHRotorIsNotASymmetricTop:
    """A methyl's barrier is carried by the free-rotor treatment of the low modes. An X-H rotor's is
    not.

    An amide C-N or an acid's O-H rotor has real, distinct rotamers; calling it a symmetric top
    would tell the model the barrier is already accounted for when it is not.
    """

    @pytest.mark.parametrize(
        ("smiles", "expected"),
        [
            ("CC(=O)N", "xh"),  # acetamide: the amide N-H
            ("CC(=O)O", "xh"),  # acetic acid: the syn/anti O-H
            ("Oc1ccccc1", "xh"),  # phenol
            ("CCO", "xh"),  # ethanol's O-H
            ("CCS", "xh"),  # a thiol
            ("Cc1ccccc1", "top"),  # toluene's methyl: a real symmetric top
            ("CC(C)(C)c1ccccc1", "top"),  # and the tert-butyl's own methyls
        ],
    )
    def test_a_hydrogen_only_end_is_a_top_only_when_it_is_symmetric(
        self, smiles: str, expected: str
    ) -> None:
        kinds = {
            torsion.kind for torsion in enumerate_torsion_candidates(smiles) if not torsion.atoms
        }
        assert expected in kinds, f"in: {smiles}  out: dihedral-less kinds {sorted(kinds)}"

    def test_an_xh_rotor_says_what_it_is_in_words(self) -> None:
        """The label is what a chemist checks the choice against, so it must not say "top"."""
        rotor = _by_kind("CC(=O)O", "xh")
        assert rotor.label == "the O-H rotation on C1", f"in: CC(=O)O  out: {rotor.label!r}"

    def test_the_dihedral_less_rotors_still_sort_last(self) -> None:
        """Ordering is part of the contract; splitting the kind must not reshuffle a list."""
        found = enumerate_torsion_candidates("CC(=O)Nc1ccccc1")
        assert [bool(torsion.atoms) for torsion in found] == sorted(
            (bool(torsion.atoms) for torsion in found), reverse=True
        )
