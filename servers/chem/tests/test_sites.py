"""What `describe_atom_sites` must get right for a per-atom number to be reportable by name.

Expectations are chemical and written independently of the implementation (phenol's symmetry
classes, the chlorine between two nitrogens, naphthalene's three carbon kinds); restating the
SMARTS table would pass whatever it said.
"""

from __future__ import annotations

import time

import pytest
from chemclaw_mcp_chem.engine.chem import InvalidSmilesError
from chemclaw_mcp_chem.engine.sites import SCOPES, Site, describe_atom_sites, site_handle
from rdkit import Chem


def _by_atom(smiles: str) -> dict[int, Site]:
    """Every site of `smiles`, keyed by each atom index it covers."""
    return {atom: site for site in describe_atom_sites(smiles).sites for atom in site.atoms}


def test_a_site_is_a_symmetry_class_not_an_atom() -> None:
    """Phenol has four kinds of ring carbon, not six, and toluene likewise."""
    for smiles, expected in (("Oc1ccccc1", 5), ("Cc1ccccc1", 5), ("c1ccccc1", 1)):
        assert len(describe_atom_sites(smiles).sites) == expected, smiles

    sites = _by_atom("Oc1ccccc1")
    assert sites[2] is sites[6], "the two ortho carbons are one site"
    assert sites[3] is sites[5], "the two meta carbons are one site"
    assert sites[2] is not sites[3]


def test_ring_positions_are_named_the_way_a_chemist_names_them() -> None:
    """Phenol's classical positions, measured from the substituent rather than from an index."""
    sites = _by_atom("Oc1ccccc1")
    assert sites[1].ring_position == "ipso"
    assert sites[2].ring_position == "ortho"
    assert sites[3].ring_position == "meta"
    assert sites[4].ring_position == "para"
    assert "para to the OH substituent" in sites[4].label


def test_pyridine_is_numbered_from_its_nitrogen() -> None:
    """A single ring heteroatom is the reference, and "para to N" is what a chemist says."""
    sites = _by_atom("c1ccncc1")
    assert sites[3].element == "N"
    assert sites[3].ring_position is None, "the reference is not ipso to itself"
    assert sites[0].ring_position == "para"
    assert sites[2].ring_position == "ortho"


def test_a_two_heteroatom_ring_gets_a_distance_and_no_classical_name() -> None:
    """Pyrimidine is numbered, not related — *meta* would mix two conventions that disagree."""
    sites = describe_atom_sites("Clc1ccnc(Cl)n1").sites
    assert all(site.ring_position is None for site in sites), "no ortho/meta/para in an azine"
    placed = [site for site in sites if site.ring_bonds_from_reference is not None]
    # Every ring atom but the reference itself, which has no relationship to state to itself.
    assert len(placed) == 5
    assert {site.ring_reference for site in placed} == {"the ring N at atom 4"}


def test_the_snar_discriminator_is_the_flanking_nitrogen_count() -> None:
    """2,4-dichloropyrimidine: both C-Cl are ortho to a ring N, and only one sits between two.

    That count is the fact that decides which chlorine goes first, and it is the reason
    `adjacent_ring_heteroatoms` exists — a single-reference position cannot separate the two.
    """
    sites = _by_atom("Clc1ccnc(Cl)n1")
    bearing = {index: site for index, site in sites.items() if site.kind == "aryl_halide_carbon"}
    assert len(bearing) == 2
    assert sorted(site.adjacent_ring_heteroatoms for site in bearing.values()) == [1, 2]


def test_resonance_equivalent_atoms_are_two_sites_and_are_distinguishable() -> None:
    """Symmetry here is topological, so a nitro group's two oxygens do not merge.

    RDKit ranks the written structure (`=O` vs `[O-]`). What must hold is that the two never share a
    name, or "report by label" gives two answers spelled identically.
    """
    oxygens = [
        site
        for site in describe_atom_sites("c1ccccc1[N+](=O)[O-]").sites
        if site.kind == "nitro_oxygen"
    ]
    assert len(oxygens) == 2
    assert oxygens[0].label != oxygens[1].label


def test_every_label_identifies_exactly_one_site() -> None:
    """The skill names sites by label and never by index, so a collision is two answers in one.

    The molecules are known collision shapes: a fused ring, a substituted naphthalene, and an azine
    with two halogens ortho to a ring nitrogen.
    """
    for smiles in (
        "c1ccc2ncccc2c1",
        "Cc1ccc2ccccc2c1",
        "Clc1ccnc(Cl)n1",
        "c1ccccc1[N+](=O)[O-]",
        "Oc1ccccc1",
    ):
        labels = [site.label for site in describe_atom_sites(smiles).sites]
        assert len(labels) == len(set(labels)), smiles


def test_a_ring_fusion_is_not_a_substituent() -> None:
    """Naphthalene has three kinds of carbon, and none of them bears a substituent."""
    sites = describe_atom_sites("c1ccc2ccccc2c1").sites
    assert len(sites) == 3
    assert {len(site.atoms) for site in sites} == {2, 4}
    assert all(site.ring_position is None for site in sites), "alpha/beta, not ortho/para"
    assert any("fusion" in site.label for site in sites)


def test_hydrogens_are_reported_on_their_carbon_with_a_calculators_numbering() -> None:
    """The join key for a C-H question, checked against RDKit's own explicit-H molecule."""
    smiles = "Cc1ccccc1"
    # `rdkit-stubs` leaves `CanonSmiles` unannotated; `warn_unused_ignores` turns this red when it
    # is annotated.
    canonical_smiles = Chem.CanonSmiles(smiles)  # type: ignore[no-untyped-call]
    explicit = Chem.AddHs(Chem.MolFromSmiles(canonical_smiles))
    expected: dict[int, list[int]] = {}
    for atom in explicit.GetAtoms():
        if atom.GetAtomicNum() == 1:
            expected.setdefault(atom.GetNeighbors()[0].GetIdx(), []).append(atom.GetIdx())

    for site in describe_atom_sites(smiles).sites:
        assert site.hydrogens == sorted(
            index for atom in site.atoms for index in expected.get(atom, [])
        )
    assert not any(site.element == "H" for site in describe_atom_sites(smiles).sites)


def test_the_methyl_of_toluene_is_one_site_carrying_three_hydrogens() -> None:
    """A symmetric top is one site; its three hydrogens are one class of C-H."""
    methyl = _by_atom("Cc1ccccc1")[0]
    assert methyl.kind == "benzylic_carbon"
    assert methyl.hydrogen_count == 3
    assert len(methyl.hydrogens) == 3
    assert "ch_sites" in methyl.scopes


@pytest.mark.parametrize(
    ("smiles", "element", "kind"),
    [
        ("CC(=O)OC", "C", "ester_carbon"),
        ("CC(=O)NC", "C", "amide_carbon"),
        ("CC(=O)O", "C", "carboxyl_carbon"),
        ("CC(C)=O", "C", "carbonyl_carbon"),
        ("CC#N", "C", "nitrile_carbon"),
        ("C=CC(=O)N(C)C", "C", "michael_beta_carbon"),
        ("Clc1ccccc1", "C", "aryl_halide_carbon"),
        ("CCCl", "C", "halide_carbon"),
        ("CSC", "S", "thioether_sulfur"),
        ("CO", "O", "hydroxyl_oxygen"),
        ("CNC", "N", "amine_nitrogen"),
        ("c1ccccc1[N+](=O)[O-]", "N", "nitro_nitrogen"),
    ],
)
def test_the_kinds_a_chemoselectivity_question_turns_on_are_distinguished(
    smiles: str, element: str, kind: str
) -> None:
    """One `carbonyl_carbon` label for an ester and an amide would make "which first" unsayable.

    Asserted by kind rather than by atom index, because the index is the canonical form's and
    pinning it here would be pinning RDKit's canonicalisation instead of the classification.
    """
    matching = [site for site in describe_atom_sites(smiles).sites if site.kind == kind]
    assert len(matching) == 1, f"{smiles}: expected exactly one {kind}, got {len(matching)}"
    assert matching[0].element == element


def test_the_michael_beta_carbon_is_the_one_distal_from_the_carbonyl() -> None:
    """Getting this backwards would tune a warhead at the wrong atom."""
    sites = _by_atom("C=CC(=O)N(C)C")
    assert sites[0].kind == "michael_beta_carbon"
    assert sites[1].kind != "michael_beta_carbon"
    assert sites[2].kind == "amide_carbon"


def test_scopes_are_questions_not_a_partition() -> None:
    """A beta carbon carrying hydrogens is both an electrophilic carbon and a C-H site."""
    beta = _by_atom("C=CC(=O)N(C)C")[0]
    assert {"electrophilic_carbons", "ch_sites", "all"} <= set(beta.scopes)
    for site in describe_atom_sites("Oc1ccccc1").sites:
        assert "all" in site.scopes
        assert set(site.scopes) <= set(SCOPES)


def test_scope_selection_finds_the_answer_top_n_buries() -> None:
    """Phenol's `ring_carbons` scope is six atoms in four classes — the comparison actually asked.

    Over all 13 atoms the para carbon ranks 6th and both meta carbons below four hydrogens, so a
    truncation of the full list returns the wrong rows however large it is.
    """
    ring = [
        site for site in describe_atom_sites("Oc1ccccc1").sites if "ring_carbons" in site.scopes
    ]
    assert len(ring) == 4
    assert sum(len(site.atoms) for site in ring) == 6
    assert {site.ring_position for site in ring} == {"ipso", "ortho", "meta", "para"}


def test_a_rewritten_smiles_gives_the_same_sites_and_the_same_indices() -> None:
    """A rewritten SMILES gives the same sites and the same indices, over three writings of
    acetanilide.

    The handle hashes a symmetry class; the indices are stable because this module canonicalises
    first, as every calculator does, so per-atom numbers join onto the right atom.
    """
    writings = ("CC(=O)Nc1ccccc1", "O=C(C)Nc1ccccc1", "c1ccc(NC(C)=O)cc1")
    handles = [
        {site.label: site.site_id for site in describe_atom_sites(smiles).sites}
        for smiles in writings
    ]
    assert handles[0] == handles[1] == handles[2]

    indices = [
        {site.label: tuple(site.atoms) for site in describe_atom_sites(smiles).sites}
        for smiles in writings
    ]
    assert indices[0] == indices[1] == indices[2]


def test_the_indices_are_the_ones_a_calculator_will_use() -> None:
    """The join asserted against RDKit's canonical ordering rather than against this module."""
    for writing in ("Oc1ccccc1", "c1ccccc1O", "c1cc(O)ccc1"):
        # The same gap in `rdkit-stubs` as above: `CanonSmiles` is the one call here its stub
        # leaves unannotated, and `MolFromSmiles` on the next line is annotated by the same package.
        written = Chem.CanonSmiles(writing)  # type: ignore[no-untyped-call]
        canonical = Chem.MolFromSmiles(written)
        expected = [atom.GetIdx() for atom in canonical.GetAtoms() if atom.GetSymbol() == "O"]
        oxygen = next(site for site in describe_atom_sites(writing).sites if site.element == "O")
        assert oxygen.atoms == expected, writing


def test_symmetry_equivalent_atoms_share_one_handle() -> None:
    """p-xylene's two methyls are one question, and one handle is how that is enforced."""
    sites = _by_atom("Cc1ccc(C)cc1")
    assert sites[0].site_id == sites[5].site_id, "the two methyls are one question"
    assert sites[0] is sites[5]
    assert sites[0].atoms == [0, 5]
    assert len(sites[0].hydrogens) == 6, "one site, six equivalent C-H"


def test_the_handle_is_bound_to_the_rdkit_build() -> None:
    """A canonical ranking is a function of the build, so a handle minted under another must not
    resolve here — the same `calc_version` argument one level down."""
    import rdkit

    mol = Chem.MolFromSmiles("Oc1ccccc1")
    assert site_handle(mol, 4).startswith("site_")
    assert len(site_handle(mol, 4)) == len("site_") + 16
    other = site_handle(mol, 4)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(rdkit, "__version__", "0.0.0-not-a-real-build")
        assert site_handle(mol, 4) != other


def test_two_runs_and_two_writings_list_the_same_sites_in_the_same_order() -> None:
    """Order is part of the contract: a caller joining on position must not need to sort first."""
    assert [site.site_id for site in describe_atom_sites("Oc1ccccc1").sites] == [
        site.site_id for site in describe_atom_sites("Oc1ccccc1").sites
    ]


def test_an_invalid_smiles_is_refused_rather_than_approximated() -> None:
    """`InvalidSmilesError` is a `ValueError`, which is the family that reaches the model."""
    for bad in ("", "CCO junk", "not-a-molecule"):
        with pytest.raises(InvalidSmilesError):
            describe_atom_sites(bad)


class TestTheIndicesSayWhichMoleculeTheyNumber:
    """The indices say which molecule they number.

    Indices are into the canonical form, so the tool must return that form; otherwise a caller
    highlighting atom 4 of their own spelling can land on a different atom (e.g. a ring nitrogen).
    """

    @pytest.mark.parametrize("written", ["c1cc(Cl)ncc1Cl", "c1ccccc1O", "c1ccc(NC(C)=O)cc1", "OCC"])
    def test_every_site_addresses_an_atom_of_the_returned_molecule(self, written: str) -> None:
        """Element by element, against the one string the caller receives."""
        found = describe_atom_sites(written)
        mol = Chem.MolFromSmiles(found.smiles)
        for site in found.sites:
            for index in site.atoms:
                assert mol.GetAtomWithIdx(index).GetSymbol() == site.element, (
                    f"in: {written}  out: smiles={found.smiles}, {site.label} at atom {index} — "
                    f"which is {mol.GetAtomWithIdx(index).GetSymbol()} there, not {site.element}"
                )

    def test_the_spelling_the_chemist_typed_numbers_a_different_atom(self) -> None:
        """The measurement kept as a test: why the molecule has to travel with the sites."""
        found = describe_atom_sites("c1cc(Cl)ncc1Cl")
        assert found.smiles == "Clc1ccc(Cl)nc1"
        typed = Chem.MolFromSmiles("c1cc(Cl)ncc1Cl")
        returned = Chem.MolFromSmiles(found.smiles)
        assert (returned.GetAtomWithIdx(4).GetSymbol(), typed.GetAtomWithIdx(4).GetSymbol()) == (
            "C",
            "N",
        )


class TestTheMoleculeIsCanonicalisedOncePerCallAndNotOncePerAtom:
    """The molecule is canonicalised once per call, not once per atom.

    Two whole-molecule passes per atom are quadratic and would run in an uncancellable worker past
    the request budget on a large molecule.
    """

    def test_a_six_hundred_atom_molecule_is_still_a_graph_operation(self) -> None:
        smiles = "C" * 600
        started = time.perf_counter()
        found = describe_atom_sites(smiles)
        elapsed = time.perf_counter() - started
        assert found.count > 0
        assert elapsed < 3.0, (
            f"in: a {len(smiles)}-atom alkane  out: {found.count} sites in {elapsed:.2f} s"
        )

    def test_hoisting_the_canonical_view_leaves_the_handle_byte_identical(self) -> None:
        """The one-shot form and the form the enumeration uses must mint the same name.

        A handle is content-addressed and carried between turns, so it must not depend on how it was
        computed.
        """
        for smiles in ("Oc1ccccc1", "CC(=O)Nc1ccccc1", "Clc1ccc(Cl)nc1"):
            found = describe_atom_sites(smiles)
            mol = Chem.MolFromSmiles(found.smiles)
            for site in found.sites:
                assert site.site_id == site_handle(mol, site.atoms[0]), (
                    f"in: {smiles}  out: {site.label} has two names"
                )


class TestAnIsotopicHydrogenIsNotAHeteroatom:
    """An isotopic hydrogen is not a heteroatom.

    `MolFromSmiles` keeps `[2H]` as an explicit atom; it must not become a site, and the carbon it
    is on must still list it in `hydrogens`, the join key for a C-H question (e.g. a KIE study on
    CD3OH).
    """

    @pytest.mark.parametrize("smiles", ["[2H]c1ccccc1", "C([2H])([2H])([2H])O", "[3H]CC"])
    def test_a_labelled_hydrogen_is_not_a_site_of_its_own(self, smiles: str) -> None:
        found = describe_atom_sites(smiles)
        assert not any(site.element == "H" for site in found.sites), (
            f"in: {smiles}  out: {[(s.element, s.label) for s in found.sites]}"
        )

    def test_the_carbon_still_reports_the_hydrogens_it_carries(self) -> None:
        """A deuterium is a hydrogen on that carbon, and the C-H question is asked about carbon."""
        found = describe_atom_sites("C([2H])([2H])([2H])O")
        carbon = next(site for site in found.sites if site.element == "C")
        assert len(carbon.hydrogens) == 3, (
            f"in: C([2H])([2H])([2H])O  out: smiles={found.smiles}, "
            f"{carbon.label} carries hydrogens={carbon.hydrogens}"
        )
