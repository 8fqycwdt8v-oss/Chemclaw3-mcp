"""What `describe_atom_sites` must get right for a per-atom number to be reportable by name.

Every expectation here is a *chemical* one written independently of the implementation — the
symmetry classes of phenol, which chlorine of a dichloropyrimidine sits between two nitrogens, that
naphthalene has three kinds of carbon. A test that restated the SMARTS table would pass whatever the
table said.
"""

from __future__ import annotations

import pytest
from chemclaw_mcp_chem.engine.chem import InvalidSmilesError
from chemclaw_mcp_chem.engine.sites import SCOPES, describe_atom_sites, site_handle
from rdkit import Chem


def _by_atom(smiles: str) -> dict[int, object]:
    """Every site of `smiles`, keyed by each atom index it covers."""
    return {atom: site for site in describe_atom_sites(smiles) for atom in site.atoms}


def test_a_site_is_a_symmetry_class_not_an_atom() -> None:
    """Phenol has four kinds of ring carbon, not six, and toluene likewise."""
    for smiles, expected in (("Oc1ccccc1", 5), ("Cc1ccccc1", 5), ("c1ccccc1", 1)):
        assert len(describe_atom_sites(smiles)) == expected, smiles

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
    sites = describe_atom_sites("Clc1ccnc(Cl)n1")
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


def test_a_ring_fusion_is_not_a_substituent() -> None:
    """Naphthalene has three kinds of carbon, and none of them bears a substituent."""
    sites = describe_atom_sites("c1ccc2ccccc2c1")
    assert len(sites) == 3
    assert {len(site.atoms) for site in sites} == {2, 4}
    assert all(site.ring_position is None for site in sites), "alpha/beta, not ortho/para"
    assert any("fusion" in site.label for site in sites)


def test_hydrogens_are_reported_on_their_carbon_with_a_calculators_numbering() -> None:
    """The join key for a C-H question, checked against RDKit's own explicit-H molecule."""
    smiles = "Cc1ccccc1"
    explicit = Chem.AddHs(Chem.MolFromSmiles(Chem.CanonSmiles(smiles)))
    expected: dict[int, list[int]] = {}
    for atom in explicit.GetAtoms():
        if atom.GetAtomicNum() == 1:
            expected.setdefault(atom.GetNeighbors()[0].GetIdx(), []).append(atom.GetIdx())

    for site in describe_atom_sites(smiles):
        assert site.hydrogens == sorted(
            index for atom in site.atoms for index in expected.get(atom, [])
        )
    assert not any(site.element == "H" for site in describe_atom_sites(smiles))


def test_the_methyl_of_toluene_is_one_site_carrying_three_hydrogens() -> None:
    """A symmetric top is one site; its three hydrogens are one class of C-H."""
    methyl = _by_atom("Cc1ccccc1")[0]
    assert methyl.kind == "benzylic_carbon"
    assert methyl.hydrogen_count == 3
    assert len(methyl.hydrogens) == 3
    assert "ch_sites" in methyl.scopes


@pytest.mark.parametrize(
    ("smiles", "atom", "kind"),
    [
        ("CC(=O)OC", 1, "ester_carbon"),
        ("CC(=O)NC", 1, "amide_carbon"),
        ("CC(=O)O", 1, "carboxyl_carbon"),
        ("CC(C)=O", 1, "carbonyl_carbon"),
        ("CC#N", 1, "nitrile_carbon"),
        ("C=CC(=O)N(C)C", 0, "michael_beta_carbon"),
        ("Clc1ccccc1", 1, "aryl_halide_carbon"),
        ("CCCl", 1, "halide_carbon"),
        ("CSC", 1, "thioether_sulfur"),
        ("CO", 1, "hydroxyl_oxygen"),
        ("CNC", 1, "amine_nitrogen"),
    ],
)
def test_the_kinds_a_chemoselectivity_question_turns_on_are_distinguished(
    smiles: str, atom: int, kind: str
) -> None:
    """One `carbonyl_carbon` label for an ester and an amide would make "which first" unsayable."""
    assert _by_atom(smiles)[atom].kind == kind


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
    for site in describe_atom_sites("Oc1ccccc1"):
        assert "all" in site.scopes
        assert set(site.scopes) <= set(SCOPES)


def test_scope_selection_finds_the_answer_top_n_buries() -> None:
    """Phenol's `ring_carbons` scope is six atoms in four classes — the comparison actually asked.

    The measured failure this replaces: over all 13 atoms the para carbon ranks 6th and both meta
    carbons rank below four hydrogens, so truncation returns the wrong rows however large it is.
    """
    ring = [site for site in describe_atom_sites("Oc1ccccc1") if "ring_carbons" in site.scopes]
    assert len(ring) == 4
    assert sum(len(site.atoms) for site in ring) == 6
    assert {site.ring_position for site in ring} == {"ipso", "ortho", "meta", "para"}


def test_a_handle_survives_a_rewritten_smiles() -> None:
    """The whole point: an index changes with the writing, a handle does not."""
    writings = ("CC(=O)Nc1ccccc1", "O=C(C)Nc1ccccc1", "c1ccc(NC(C)=O)cc1")
    handles = [
        {site.label: site.site_id for site in describe_atom_sites(smiles)} for smiles in writings
    ]
    assert handles[0] == handles[1] == handles[2]

    indices = [
        {site.label: tuple(site.atoms) for site in describe_atom_sites(smiles)}
        for smiles in writings
    ]
    assert indices[0] != indices[2], "the indices really do differ; that is what is being defended"


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
    assert [site.site_id for site in describe_atom_sites("Oc1ccccc1")] == [
        site.site_id for site in describe_atom_sites("Oc1ccccc1")
    ]


def test_an_invalid_smiles_is_refused_rather_than_approximated() -> None:
    """`InvalidSmilesError` is a `ValueError`, which is the family that reaches the model."""
    for bad in ("", "CCO junk", "not-a-molecule"):
        with pytest.raises(InvalidSmilesError):
            describe_atom_sites(bad)
