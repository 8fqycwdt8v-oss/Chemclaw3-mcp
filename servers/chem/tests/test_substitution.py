"""What `enumerate_substitutions` must get right for a ranking over its set to mean anything.

Two kinds of assertion. The contract ones are what Chemclaw3's template reads: `smiles` and
`labels` passed straight to `rank_species`, so they must be positional and every member must be an
isomer of every other — a ranking over two formulas is not a ranking. The chemistry ones are the
places this enumerator could produce a well-formed set for the wrong question: the input missing
from a `move` set or present in an `add` one, a group bonded through the wrong atom, an aliphatic
position offered as a regioisomer, a ring nitrogen stripped of its group.

The cost bounds are driven on the shapes `engine/substitution.py` derived them from, and asserted
prompt: a refusal costs a parse and a count, never the enumeration it refuses.
"""

from __future__ import annotations

import time

import pytest
from chemclaw_mcp_chem.engine import substitution
from chemclaw_mcp_chem.engine.chem import InvalidSmilesError
from chemclaw_mcp_chem.engine.sites import describe_atom_sites
from chemclaw_mcp_chem.engine.substitution import (
    MAX_SUBSTITUTIONS,
    enumerate_substitution_set,
)
from rdkit import Chem
from rdkit.Chem.rdMolDescriptors import CalcMolFormula

#: A refusal costs a parse and a count; generous against a slow, shared runner.
PROMPT_SECONDS = 2.0


def _canonical(smiles: str) -> str:
    return str(Chem.MolToSmiles(Chem.MolFromSmiles(smiles)))


def _formulas(smiles: list[str]) -> set[str]:
    return {CalcMolFormula(Chem.MolFromSmiles(one)) for one in smiles}


def test_moving_a_methyl_gives_the_three_cresols_with_the_input_first() -> None:
    """p-Cresol's positional isomers are o-, m- and p-cresol, and the one drawn leads the set."""
    result = enumerate_substitution_set("Cc1ccc(O)cc1")
    assert result.mode == "move"
    assert result.smiles[0] == result.parent == _canonical("Cc1ccc(O)cc1")
    assert result.labels[0] == "as given"
    assert result.sites[0] is None
    assert set(result.smiles) == {
        _canonical("Cc1ccc(O)cc1"),
        _canonical("Cc1cccc(O)c1"),
        _canonical("Cc1ccccc1O"),
    }
    assert result.count == len(result.smiles) == len(result.labels) == len(result.sites)


def test_a_move_set_is_one_formula_and_so_is_an_add_set() -> None:
    """What makes the set rankable: every member is an isomer of every other."""
    moved = enumerate_substitution_set("Cc1ccc(Cl)c(OC)c1")
    assert len(_formulas(moved.smiles)) == 1
    added = enumerate_substitution_set("Oc1ccccc1", "*[N+](=O)[O-]", "add")
    assert len(_formulas(added.smiles)) == 1
    assert _formulas(added.smiles) != _formulas([added.parent])


def test_an_add_set_is_the_regioisomers_and_leaves_out_the_starting_material() -> None:
    """Nitrating phenol has three places to go, and phenol itself is not a nitration product."""
    result = enumerate_substitution_set("Oc1ccccc1", "*[N+](=O)[O-]", "add")
    assert result.mode == "add"
    assert result.parent not in result.smiles
    assert set(result.smiles) == {
        _canonical("O=[N+]([O-])c1ccccc1O"),
        _canonical("O=[N+]([O-])c1cccc(O)c1"),
        _canonical("O=[N+]([O-])c1ccc(O)cc1"),
    }
    assert result.groups == ["*[N+](=O)[O-]"]
    by_smiles = dict(zip(result.smiles, result.labels, strict=True))
    assert "para" in by_smiles[_canonical("O=[N+]([O-])c1ccc(O)cc1")]


def test_the_landing_sites_are_the_handles_describe_sites_reports_for_the_parent() -> None:
    """`sites` joins onto `describe_sites`, which is the only reason to return a handle at all."""
    result = enumerate_substitution_set("Oc1ccccc1", "Cl", "add")
    known = {site.site_id for site in describe_atom_sites(result.parent).sites}
    assert set(result.sites) <= known
    assert len(set(result.sites)) == result.count


def test_the_answer_does_not_depend_on_how_the_molecule_was_written() -> None:
    """Two spellings of one compound, one set in one order."""
    first = enumerate_substitution_set("Cc1ccc(O)cc1")
    second = enumerate_substitution_set("Oc1ccc(C)cc1")
    assert first.smiles == second.smiles
    assert first.labels == second.labels


def test_a_group_bonds_through_its_first_atom_so_methoxy_and_hydroxymethyl_differ() -> None:
    """`OC` is methoxy and `CO` is hydroxymethyl; reading both as "CH3O" would swap compounds."""
    methoxy = enumerate_substitution_set("c1ccncc1", "OC", "add")
    hydroxymethyl = enumerate_substitution_set("c1ccncc1", "CO", "add")
    assert methoxy.groups == ["*OC"]
    assert hydroxymethyl.groups == ["*CO"]
    assert _canonical("COc1ccncc1") in methoxy.smiles
    assert _canonical("OCc1ccncc1") in hydroxymethyl.smiles
    assert not set(methoxy.smiles) & set(hydroxymethyl.smiles)


def test_a_group_with_no_hydrogen_on_its_first_atom_is_refused_naming_the_star() -> None:
    """Nitro written bare bonds through a nitrogen with no hydrogen to give up."""
    with pytest.raises(ValueError, match=r"\*\[N\+\]\(=O\)\[O-\]"):
        enumerate_substitution_set("Oc1ccccc1", "[N+](=O)[O-]", "add")
    with pytest.raises(ValueError, match="exactly one"):
        enumerate_substitution_set("Oc1ccccc1", "*C*", "add")


def test_add_without_a_group_is_refused() -> None:
    with pytest.raises(ValueError, match="needs `substituent`"):
        enumerate_substitution_set("Oc1ccccc1", None, "add")


def test_a_named_group_moves_alone_and_an_absent_one_is_refused_listing_what_is_there() -> None:
    """Asking to move the methoxy of 4-methylanisole moves only the methoxy."""
    result = enumerate_substitution_set("COc1ccc(C)cc1", "OC")
    assert result.groups == ["*OC"]
    assert all(label.startswith("*OC moved") for label in result.labels[1:])
    with pytest.raises(ValueError, match=r"\*C, \*OC"):
        enumerate_substitution_set("COc1ccc(C)cc1", "Cl")


def test_only_aromatic_positions_are_offered() -> None:
    """6-Methyltetralin: the methyl may visit the benzene ring's C-H and never the CH2 ring."""
    result = enumerate_substitution_set("Cc1ccc2c(c1)CCCC2")
    for smiles in result.smiles:
        mol = Chem.MolFromSmiles(smiles)
        methyl = next(
            atom
            for atom in mol.GetAtoms()
            if atom.GetSymbol() == "C" and atom.GetDegree() == 1 and not atom.IsInRing()
        )
        assert methyl.GetNeighbors()[0].GetIsAromatic(), smiles
    # 5- and 6-methyltetralin: the ring's 7 and 8 positions are 6 and 5 again, by its mirror.
    assert set(result.smiles) == {_canonical("Cc1ccc2c(c1)CCCC2"), _canonical("Cc1cccc2c1CCCC2")}


def test_a_group_on_a_ring_nitrogen_stays_there_and_the_azole_question_is_still_answered() -> None:
    """1-Methyl-3-phenylpyrazole: the N-methyl is never moved; the phenyl reaches C5 and C4.

    Moving the methyl off the nitrogen would leave an N-H pyrazole — a tautomer question, not a
    positional one — and the regiochemistry chemists actually ask about here (1,3- against
    1,5-disubstitution after N-alkylation) is the phenyl's C3/C5 choice, which this set carries.
    """
    result = enumerate_substitution_set("Cn1ccc(-c2ccccc2)n1")
    assert _canonical("Cn1nccc1-c1ccccc1") in result.smiles
    assert _canonical("Cn1cc(-c2ccccc2)cn1") in result.smiles
    for smiles in result.smiles:
        assert Chem.MolFromSmiles(smiles).HasSubstructMatch(Chem.MolFromSmarts("[CH3]n")), smiles


def test_a_molecule_with_nothing_to_move_is_its_own_only_member() -> None:
    """Benzene and toluene have no positional isomers; the set is the input, not an error."""
    assert enumerate_substitution_set("c1ccccc1").smiles == ["c1ccccc1"]
    assert enumerate_substitution_set("Cc1ccccc1").count == 1


def test_an_add_onto_a_molecule_with_no_aromatic_ch_is_refused() -> None:
    with pytest.raises(ValueError, match="no aromatic C-H"):
        enumerate_substitution_set("CCCCCC", "Cl", "add")


def test_a_string_that_is_not_a_molecule_is_refused() -> None:
    with pytest.raises(InvalidSmilesError):
        enumerate_substitution_set("not a molecule")
    with pytest.raises(InvalidSmilesError):
        enumerate_substitution_set("c1ccccc1", "C(", "add")


def test_a_molecule_past_the_heavy_atom_bound_is_refused_before_it_is_canonicalised() -> None:
    """A long chain on toluene: ten candidates, and 4.9 s of fixed cost at 1,992 atoms."""
    chain = "Cc1ccc(" + "C" * 1985 + ")cc1"
    started = time.perf_counter()
    with pytest.raises(ValueError, match="CHEMCLAW_CHEM_MAX_SUBSTITUTION_HEAVY_ATOMS"):
        enumerate_substitution_set(chain)
    assert time.perf_counter() - started < PROMPT_SECONDS


def test_a_series_past_the_cost_bound_is_refused_before_any_product_is_built() -> None:
    """A 30-ring polyphenylene: 180 atoms, inside the size bound, ~300 candidates — priced out."""
    polyphenylene = "c1ccc(cc1)" * 29 + "c1ccccc1"
    started = time.perf_counter()
    with pytest.raises(ValueError, match="CHEMCLAW_CHEM_MAX_SUBSTITUTION_CANDIDATE_ATOM_PRODUCT"):
        enumerate_substitution_set(polyphenylene)
    assert time.perf_counter() - started < PROMPT_SECONDS


def test_a_series_past_the_output_cap_is_refused_rather_than_truncated() -> None:
    """A 2,5-linked oligopyridine of 16 rings has 75 positional isomers inside the cost bound."""
    oligopyridine = "c1ccc(nc1)" * 15 + "c1ccccn1"
    with pytest.raises(ValueError, match=f"above the limit of {MAX_SUBSTITUTIONS}"):
        enumerate_substitution_set(oligopyridine)


def test_the_bounds_are_the_ones_the_frontier_was_measured_against() -> None:
    """The defaults the module docstring derives, so a changed default re-reads its derivation."""
    assert substitution.MAX_SUBSTITUTION_HEAVY_ATOMS == 250
    assert substitution.MAX_SUBSTITUTION_CANDIDATE_ATOM_PRODUCT == 20_000
    nilotinib = "Cc1cn(-c2cc(NC(=O)c3ccc(C)c(Nc4nccc(-c5cccnc5)n4)c3)cc(C(F)(F)F)c2)cn1"
    assert enumerate_substitution_set(nilotinib).count > 1, "a real drug must stay inside both"
