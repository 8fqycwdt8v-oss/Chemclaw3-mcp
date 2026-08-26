"""What the species enumerators must get right for the expensive half to mean anything.

Every assertion here is either a cross-repository contract (Chemclaw3 reads these field names) or a
defect that was **measured during implementation** rather than anticipated. The three in the second
class are called out in their own docstrings, because a test whose reason is "it was wrong once" is
worth more than one whose reason is "it seemed important".
"""

from __future__ import annotations

import pytest
from chemclaw_mcp_chem.engine.chem import InvalidSmilesError
from chemclaw_mcp_chem.engine.species import (
    MAX_STEREOISOMERS,
    describe_molecule,
    enumerate_degradant_candidates,
    enumerate_microstates,
    enumerate_stereoisomer_set,
    enumerate_tautomer_set,
)

# A substrate per transform, and the product each must reach. Written as data because the whole
# claim of `enumerate_degradants` is that its transforms produce real chemistry — an unmapped
# reaction SMARTS silently drops atoms, which is exactly how the first version of this table
# produced fragments instead of molecules and looked fine doing it.
_TRANSFORM_CASES: tuple[tuple[str, str, str], ...] = (
    ("CN(C)c1ccccc1", "N-oxidation", "C[N+](C)([O-])c1ccccc1"),
    ("CSc1ccccc1", "S-oxidation to sulfoxide", "CS(=O)c1ccccc1"),
    ("CCc1ccccc1", "benzylic hydroxylation", "CC(O)c1ccccc1"),
    ("CC(O)c1ccccc1", "secondary alcohol to ketone", "CC(=O)c1ccccc1"),
    ("O=Cc1ccccc1", "aldehyde to carboxylic acid", "O=C(O)c1ccccc1"),
    ("CC(=O)Nc1ccccc1", "amide hydrolysis", "Nc1ccccc1"),
    ("CCOC(=O)c1ccccc1", "ester hydrolysis", "O=C(O)c1ccccc1"),
    ("N#Cc1ccccc1", "nitrile hydration", "NC(=O)c1ccccc1"),
    ("OC(=O)c1ccccc1", "decarboxylation", "c1ccccc1"),
)


@pytest.mark.parametrize(("substrate", "transform", "product"), _TRANSFORM_CASES)
def test_every_transform_reaches_the_product_a_chemist_would_name(
    substrate: str, transform: str, product: str
) -> None:
    """Each degradation transform, on a substrate it is meant for, gives the expected structure.

    **This is the test the transform table cannot be trusted without.** A reaction SMARTS with
    incomplete atom mapping parses, runs, and returns a *fragment* — RDKit drops what the product
    template does not name. Four of the eleven transforms were written that way at first: the
    secondary-alcohol oxidation carried no maps at all, and decarboxylation kept the carboxyl carbon
    and dropped the R group. Every one produced a well-formed SMILES for the wrong molecule.
    """
    found = {
        candidate.smiles
        for candidate in enumerate_degradant_candidates(substrate).degradants
        if candidate.transform == transform
    }
    assert product in found, f"{transform} on {substrate} gave {sorted(found)}, wanted {product}"


def test_an_acid_deprotonates_at_the_hydroxyl_and_not_at_the_carbonyl() -> None:
    """The ionisable atom is the one carrying the proton, which is not the first hetero in a match.

    **Measured, not reasoned about.** The site finder first took "the first N, O or S in the
    match", and for `[CX3](=O)[OX2H1]` that is the *carbonyl* oxygen — which carries no hydrogen,
    so every carboxylic acid was located and then silently failed to ionise. Beta-alanine came back
    with its ammonium form and no carboxylate: two species where there should be three, with
    nothing raised. Every acid pattern is now written to start on the ionisable atom.
    """
    states = enumerate_microstates("NCCC(=O)O")

    assert "NCCC(=O)[O-]" in states.smiles, "the carboxylate is missing"
    assert "[NH3+]CCC(=O)O" in states.smiles, "the ammonium is missing"
    assert states.smiles[0] == states.parent, "the input must be first"
    assert len(states.labels) == len(states.smiles), "labels are positional against smiles"


def test_an_unspecified_parent_is_not_one_of_its_own_stereoisomers() -> None:
    """A structure with open centres is the question, not a member of the answer.

    **Measured**: prepending the parent unconditionally gave `CC(Cl)C(Br)C` five species for a
    molecule with two centres. That matters beyond the count — `rank_species` populates over
    exactly the list it is given, so the extra species would have been embedded, optimised and
    assigned a Boltzmann population of its own.
    """
    isomers = enumerate_stereoisomer_set("CC(Cl)C(Br)C")

    assert isomers.count == 4, f"two open centres is four isomers, got {isomers.smiles}"
    assert isomers.parent not in isomers.smiles


def test_a_fully_specified_structure_comes_back_as_itself() -> None:
    """Only *unassigned* centres are expanded; a drawn configuration is a claim, not a question."""
    isomers = enumerate_stereoisomer_set("C[C@H](Cl)[C@H](Br)C")

    assert isomers.count == 1
    assert isomers.smiles == [isomers.parent]


def test_a_tautomer_set_contains_the_form_it_was_asked_about() -> None:
    """The universe a ranking normalizes over must include the input, or it answers a different
    question: "which form dominates" is unanswerable when the form you have is not a candidate."""
    forms = enumerate_tautomer_set("CC(=O)CC(C)=O")

    assert forms.smiles[0] == forms.parent
    assert forms.count > 1, "acetylacetone is the textbook tautomeric case"
    assert len(set(forms.smiles)) == forms.count, "the set must be de-duplicated"


def test_an_enumeration_past_its_cap_refuses_rather_than_truncating() -> None:
    """A truncated set silently redefines the universe a population is normalized over.

    The refusal names what to do instead, and is a `ValueError` so `connector_app` lets the wording
    reach the caller rather than replacing it with a generic notice.
    """
    # Seven unassigned centres is 128 isomers. Measured rather than assumed: six centres is
    # exactly 64 and passes, which is the boundary this substrate sits one step beyond.
    crowded = "CC(Cl)C(Br)C(F)C(I)C(Cl)C(Br)C(F)C"
    with pytest.raises(ValueError, match=f"above the limit of {MAX_STEREOISOMERS}") as raised:
        enumerate_stereoisomer_set(crowded)
    assert "Narrow the molecule" in str(raised.value), "a refusal must say what to do instead"


def test_a_degradant_set_excludes_the_parent() -> None:
    """`count` must read as "how many liabilities", not one more.

    The opposite convention from the three enumerators above, and deliberately so: those produce
    *forms of* the input, this produces *products from* it.
    """
    proposals = enumerate_degradant_candidates("CC(=O)Nc1ccccc1")

    assert proposals.count == len(proposals.degradants)
    assert proposals.parent not in {item.smiles for item in proposals.degradants}
    assert all(item.transform for item in proposals.degradants), "a proposal must be arguable"


def test_topology_answers_whether_a_search_would_find_anything() -> None:
    """The fields the calling skills branch on, asserted on molecules with known answers."""
    rigid = describe_molecule("c1ccccc1")
    assert rigid.rotatable_bonds == 0
    assert rigid.unassigned_stereocentres == 0
    assert rigid.tautomer_count == 1, "benzene has no tautomer question"

    tautomeric = describe_molecule("CC(=O)CC(C)=O")
    assert tautomeric.tautomer_count > 1

    amphoteric = describe_molecule("NCCC(=O)O")
    assert amphoteric.ionisable_acidic_sites == 1
    assert amphoteric.ionisable_basic_sites == 1


def test_a_string_that_is_not_a_molecule_is_refused_by_every_enumerator() -> None:
    """`require_molecule`'s strictness reaches these too — a truncating parse would enumerate the
    forms of a smaller molecule than the caller submitted."""
    for call in (
        enumerate_tautomer_set,
        enumerate_microstates,
        enumerate_stereoisomer_set,
        describe_molecule,
    ):
        with pytest.raises(InvalidSmilesError):
            call("CCO junk")
