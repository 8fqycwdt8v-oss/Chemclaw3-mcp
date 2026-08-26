"""What a bond-cleavage enumeration must get right for the survey that consumes it.

The field names asserted here are a **cross-repository contract**: Chemclaw3's `BondCleavageSpec`
has `atoms`, `bond` and `fragments`, and its `bond-strength-survey` template passes this tool's
output through unchanged, with a comment recording that a template cannot rename a field. A
near-miss here does not fail loudly — it needs a model in the middle to re-type the list, which is
the failure this file exists to make impossible.
"""

from __future__ import annotations

import pytest
from chemclaw_mcp_chem.engine.chem import InvalidSmilesError
from chemclaw_mcp_chem.engine.cleavage import MAX_CLEAVAGES, enumerate_cleavages
from rdkit import Chem

# The three field names Chemclaw3's `BondCleavageSpec` declares. Written out rather than imported,
# because the whole point is that the two repositories agree without sharing a package.
_CONTRACT = {"atoms", "bond", "fragments"}


def test_a_cleavage_carries_exactly_the_fields_the_survey_job_declares() -> None:
    """The cross-repository contract, asserted on the serialised form the wire actually carries."""
    entry = enumerate_cleavages("CCO").cleavages[0].model_dump()

    assert set(entry) == _CONTRACT, (
        f"the survey job takes {sorted(_CONTRACT)}; a field added or renamed here needs a model in "
        "the middle to re-type the list, which is what passing it through by value avoids"
    )
    assert len(entry["atoms"]) == 2 and len(entry["fragments"]) == 2


def test_a_homolysis_gives_two_radicals_with_the_open_shell_explicit() -> None:
    """The fragments must say they are radicals, so no spin state has to be declared separately.

    That is the calculation's own requirement — Chemclaw3's job spec records it as the reason the
    cleavages arrive rather than being derived there.
    """
    ethanol = enumerate_cleavages("CCO", "homolytic")
    co = next(entry for entry in ethanol.cleavages if entry.bond == "C-O")

    for fragment in co.fragments:
        mol = Chem.MolFromSmiles(fragment)
        assert mol is not None, f"{fragment} is not a molecule"
        assert sum(atom.GetNumRadicalElectrons() for atom in mol.GetAtoms()) == 1, (
            f"{fragment} carries no radical electron; the open shell must be explicit"
        )
        assert Chem.GetFormalCharge(mol) == 0, "a homolysis produces neutral radicals"


def test_a_heterolysis_gives_the_electrons_to_the_more_electronegative_end() -> None:
    """Which fragment keeps the electrons is decided here, not left to the caller.

    C-O breaking heterolytically gives a carbocation and an alkoxide, never the reverse. Leaving it
    to traversal order would give two different answers for one bond.
    """
    ethanol = enumerate_cleavages("CCO", "heterolytic")
    co = next(entry for entry in ethanol.cleavages if entry.bond == "C-O")
    charges = {
        Chem.GetFormalCharge(Chem.MolFromSmiles(fragment)): fragment for fragment in co.fragments
    }

    assert set(charges) == {1, -1}, f"a heterolysis is an ion pair, got {co.fragments}"
    assert "[O-]" in charges[-1], "oxygen is more electronegative than carbon and keeps the pair"


def test_symmetry_equivalent_bonds_collapse_to_one_entry() -> None:
    """Three methyl C-H bonds are one bond by symmetry, and three reaction energies by cost.

    **The cost is the point and the correctness follows it.** Enumerated separately, the survey
    pays one reaction energy each for three identical numbers, and then reports three joint-weakest
    bonds — which a reader cannot distinguish from a genuine degeneracy.
    """
    ethanol = enumerate_cleavages("CCO")

    assert ethanol.count == 5, (
        "ethanol has C-C, C-O, O-H and two distinct C-H environments; "
        f"got {[entry.bond for entry in ethanol.cleavages]}"
    )
    assert [entry.bond for entry in ethanol.cleavages].count("C-H") == 2


def test_ring_bonds_are_not_offered() -> None:
    """Breaking one ring bond gives a biradical, not two fragments.

    The survey computes a balanced reaction per bond and cannot express a one-piece product.
    """
    benzene = enumerate_cleavages("c1ccccc1")

    assert all(entry.bond == "C-H" for entry in benzene.cleavages), (
        f"only the exocyclic C-H bonds are breakable, got {[e.bond for e in benzene.cleavages]}"
    )
    assert benzene.count == 1, "all six C-H are one by symmetry"


def test_hydrogens_are_broken_even_though_the_input_leaves_them_implicit() -> None:
    """A C-H is the bond a radical-abstraction question is usually about."""
    bonds = {entry.bond for entry in enumerate_cleavages("CCO").cleavages}

    assert "C-H" in bonds and "O-H" in bonds


def test_a_molecule_past_the_cap_refuses_rather_than_ranking_a_subset() -> None:
    """A weakest bond found among "the first 48 the traversal reached" is not the weakest bond."""
    # A long chain terminated by an OH, so the ends are inequivalent and symmetry collapses
    # little. Measured: `"C" * 40` is only 40 distinct bonds — a symmetric alkane folds in half,
    # which is why the obvious "make it big" substrate does not reach the cap at all.
    with pytest.raises(ValueError, match=f"above the limit of {MAX_CLEAVAGES}") as raised:
        enumerate_cleavages("C" * 25 + "O")
    assert "Name the bonds you want" in str(raised.value)


def test_a_string_that_is_not_a_molecule_is_refused() -> None:
    with pytest.raises(InvalidSmilesError):
        enumerate_cleavages("CCO junk")


class TestTheIndicesAddressTheMoleculeThatIsReturned:
    """`atoms` and `parent` must describe one molecule, because the caller only receives `parent`.

    This is the failure `describe_atom_sites` records for phenol and `torsion_handle` exists to
    remove, recurring in the one enumerator that had neither guard: the indices were into
    `AddHs(<the caller's spelling>)` while `parent` was the canonical SMILES, and the field
    description claimed the opposite. Chemclaw3's survey copies `atoms` straight into
    `DissociatedBond.atoms`, which is what the chemist reads beside "this is the weakest bond".
    """

    @pytest.mark.parametrize(
        ("written", "canonical"),
        [
            ("OCC", "CCO"),
            ("c1ccc(NC(C)=O)cc1", "CC(=O)Nc1ccccc1"),
            ("OC(=O)c1ccccc1", "O=C(O)c1ccccc1"),
        ],
    )
    def test_the_bond_name_reads_the_same_in_the_returned_molecule(
        self, written: str, canonical: str
    ) -> None:
        """`enumerate_bond_cleavages("OCC")` reported `atoms=[0, 1], bond="O-C"`, and atoms 0 and 1
        of the returned `CCO` are C0-C1 — a different bond, really bonded, in range, no error.
        """
        found = enumerate_cleavages(written)
        assert found.parent == canonical, f"in: {written}  out: parent={found.parent}"
        mol = Chem.AddHs(Chem.MolFromSmiles(found.parent))
        for entry in found.cleavages:
            begin, end = (mol.GetAtomWithIdx(index) for index in entry.atoms)
            read_back = f"{begin.GetSymbol()}-{end.GetSymbol()}"
            assert mol.GetBondBetweenAtoms(*entry.atoms) is not None, (
                f"in: {written}  out: parent={found.parent}, atoms={entry.atoms} are not bonded"
            )
            assert read_back == entry.bond, (
                f"in: {written}  out: parent={found.parent}, {entry.bond} at atoms "
                f"{entry.atoms} — which reads as {read_back} in the molecule that was returned"
            )

    def test_two_spellings_of_one_compound_give_the_same_indices(self) -> None:
        """The canonical form is the join, so how the caller wrote it cannot move an index."""
        first = enumerate_cleavages("OCC")
        second = enumerate_cleavages("CCO")
        assert [entry.atoms for entry in first.cleavages] == [
            entry.atoms for entry in second.cleavages
        ], f"in: OCC / CCO  out: {first.parent} / {second.parent}"
