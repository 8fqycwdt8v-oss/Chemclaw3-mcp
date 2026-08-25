"""The role rules, on reactions a chemist would recognise.

Every assertion here is a chemistry claim, so each test says which one. The two that matter most
are the context-dependent pair — a phosphine is a ligand or a reagent depending on the rest of the
flask — because they are the reason this operates on a reaction rather than on a molecule, and
because getting either wrong puts a wrong row at the top of a frequency table somebody quotes.

These run without the optional models, which is the point: the classification below is what a
deployment gets from RDKit alone, and the atom map only refines it.
"""

from __future__ import annotations

import pytest
from chemclaw_mcp_rxnlabel.engine import agents, roles, species

BUCHWALD = (
    "Brc1ccccc1.NC1CCCCC1"
    ">CC(C)(C)P(C(C)(C)C)C(C)(C)C.CC(C)(C)[O-].CC#N.CC(=O)O[Pd]OC(C)=O"
    ">c1ccc(NC2CCCCC2)cc1"
)

SUZUKI = (
    "COc1ccc(Br)cc1.OB(O)c1ccccc1"
    ">c1ccc(P(c2ccccc2)c2ccccc2)cc1.[O-]C(=O)[O-].C1CCOC1.[Pd]"
    ">COc1ccc(-c2ccccc2)cc1"
)

# Triphenylphosphine on the left, stoichiometrically, with no metal anywhere: a Mitsunobu.
MITSUNOBU = (
    "OCc1ccccc1.OC(=O)c1ccccc1.c1ccc(P(c2ccccc2)c2ccccc2)cc1.CCOC(=O)/N=N/C(=O)OCC"
    ">C1CCOC1"
    ">O=C(OCc1ccccc1)c1ccccc1"
)


def _roles_of(reaction: str, structures: list[str]) -> dict[str, str]:
    """The assigned role of each structure, keyed by the structure."""
    return dict(zip(structures, roles.assign(reaction, structures), strict=True))


def test_a_buchwald_separates_ligand_base_solvent_and_catalyst() -> None:
    """The four agent roles the recorded vocabulary collapses into one word.

    An ELN records all four of these as `reagent` or `solvent`; the question "which ligands were
    used for Buchwald couplings" is answerable only because they are told apart here.
    """
    assigned = _roles_of(
        BUCHWALD,
        [
            "Brc1ccccc1",
            "NC1CCCCC1",
            "CC(C)(C)P(C(C)(C)C)C(C)(C)C",
            "CC(C)(C)[O-]",
            "CC#N",
            "CC(=O)O[Pd]OC(C)=O",
            "c1ccc(NC2CCCCC2)cc1",
        ],
    )
    assert assigned["CC(C)(C)P(C(C)(C)C)C(C)(C)C"] == roles.LIGAND
    assert assigned["CC(C)(C)[O-]"] == roles.BASE
    assert assigned["CC#N"] == roles.SOLVENT
    assert assigned["CC(=O)O[Pd]OC(C)=O"] == roles.CATALYST
    assert assigned["Brc1ccccc1"] == roles.STARTING_MATERIAL
    assert assigned["NC1CCCCC1"] == roles.STARTING_MATERIAL
    assert assigned["c1ccc(NC2CCCCC2)cc1"] == roles.PRODUCT


def test_a_suzuki_separates_the_same_four_with_different_structures() -> None:
    """Carbonate as the base, THF as the solvent, PPh3 as the ligand, bare Pd as the catalyst."""
    assigned = _roles_of(
        SUZUKI,
        [
            "c1ccc(P(c2ccccc2)c2ccccc2)cc1",
            "[O-]C(=O)[O-]",
            "C1CCOC1",
            "[Pd]",
            "OB(O)c1ccccc1",
        ],
    )
    assert assigned["c1ccc(P(c2ccccc2)c2ccccc2)cc1"] == roles.LIGAND
    assert assigned["[O-]C(=O)[O-]"] == roles.BASE
    assert assigned["C1CCOC1"] == roles.SOLVENT
    assert assigned["[Pd]"] == roles.CATALYST
    assert assigned["OB(O)c1ccccc1"] == roles.STARTING_MATERIAL


def test_the_same_phosphine_is_a_ligand_with_a_metal_and_not_without_one() -> None:
    """The chemistry claim this server's shape exists for.

    Triphenylphosphine is a ligand in a Suzuki and a stoichiometric reagent in a Mitsunobu. The
    structure is byte-identical; only the rest of the flask distinguishes them. A per-molecule
    classifier cannot get this right in both places, which is why `assign` takes a reaction.
    """
    ppd = "c1ccc(P(c2ccccc2)c2ccccc2)cc1"
    assert _roles_of(SUZUKI, [ppd])[ppd] == roles.LIGAND
    assert _roles_of(MITSUNOBU, [ppd])[ppd] != roles.LIGAND


def test_a_ferrocenyl_phosphine_is_a_ligand_and_not_a_catalyst() -> None:
    """dppf contains iron and is a ligand — so the ligand rule must be consulted before the metal
    one.

    The failure this pins is a plausible ordering bug: "contains a transition metal, therefore
    catalyst" is right for Pd(OAc)2 and wrong for every ferrocene-backboned ligand on the shelf,
    which would then be counted as catalysts and never as ligands.
    """
    # One ferrocenyl phosphine arm, in the form RDKit reads: cyclopentadienide as an
    # aromatic anion. The full dppf has two; one is enough to state the rule.
    dppf = "[Fe+2].c1ccc(P(c2ccccc2)[c-]2cccc2)cc1"
    reaction = f"Brc1ccccc1.NC1CCCCC1>{dppf}.CC(=O)O[Pd]OC(C)=O>c1ccc(NC2CCCCC2)cc1"
    assert agents.is_ligand(dppf, agents.ReactionContext(has_transition_metal=True))
    assert _roles_of(reaction, [dppf])[dppf] == roles.LIGAND


def test_a_substrate_amine_is_not_called_a_base() -> None:
    """A tertiary amine is a base *and* an enormous fraction of the corpus's substrates.

    The guard is that the base rule is consulted only for a species already known not to be a
    substrate. Without it, half of medicinal chemistry's products would be counted as bases.
    """
    reaction = "CN(C)c1ccc(Br)cc1.OB(O)c1ccccc1>[Pd]>CN(C)c1ccc(-c2ccccc2)cc1"
    assigned = _roles_of(reaction, ["CN(C)c1ccc(Br)cc1", "CN(C)c1ccc(-c2ccccc2)cc1"])
    assert assigned["CN(C)c1ccc(Br)cc1"] == roles.STARTING_MATERIAL
    assert assigned["CN(C)c1ccc(-c2ccccc2)cc1"] == roles.PRODUCT


def test_a_grignard_is_not_a_catalyst() -> None:
    """Main-group organometallics are reagents. Calling one a catalyst puts it at the top of every
    "catalysts used" table."""
    assert not agents.is_metal_complex("C[Mg]Br")
    assert not agents.is_metal_complex("CCCC[Li]")


def test_a_species_the_reaction_does_not_contain_is_unknown_not_guessed() -> None:
    """`unknown` means the caller's record and the reaction string disagree — a fact worth keeping.

    Inventing a role would hide a mismatch between two things that are supposed to describe the
    same flask.
    """
    assigned = _roles_of(BUCHWALD, ["CCCCCCCCCCCCCCCC"])
    assert assigned["CCCCCCCCCCCCCCCC"] == roles.UNKNOWN


def test_roles_are_matched_by_structure_and_not_by_position() -> None:
    """The caller's ordinals come from its own record, which orders species differently.

    A record lists inputs then outcomes; the reaction string groups the agents in the middle. Any
    reaction with a solvent has the two orders disagreeing, so a positional match would mislabel
    all of them — this asserts the answer is invariant under shuffling the request.
    """
    structures = ["CC#N", "c1ccc(NC2CCCCC2)cc1", "CC(=O)O[Pd]OC(C)=O", "Brc1ccccc1"]
    forwards = _roles_of(BUCHWALD, structures)
    backwards = _roles_of(BUCHWALD, list(reversed(structures)))
    assert forwards == backwards


def test_an_unreadable_reaction_yields_unknown_for_everything() -> None:
    """A malformed record labels nothing rather than labelling it wrongly."""
    assert roles.assign("not a reaction", ["CCO"]) == [roles.UNKNOWN]


@pytest.mark.parametrize(("name", "smarts"), species.FUNCTIONAL_GROUPS)
def test_every_functional_group_pattern_compiles(name: str, smarts: str) -> None:
    """Each pattern individually, because `_matches_any` skips one that does not compile.

    That leniency is right at runtime — one bad pattern must not fail every classification — and it
    means a typo would silently narrow the vocabulary. This is where it is caught instead.
    """
    from rdkit import Chem

    assert Chem.MolFromSmarts(smarts) is not None, f"{name} has an uncompilable SMARTS"


def test_a_multi_component_species_is_matched_component_wise() -> None:
    """A salt or a complex is one species the caller charged and several dot-separated tokens.

    Until this was caught on dppf, every ferrocenyl phosphine and every alkali-metal salt came back
    `unknown`: the whole string was compared against the slot's individual tokens and matched
    neither. The rule is that a species belongs to a slot when *every* component is written there —
    which reduces to plain membership for the ordinary single-component case.
    """
    salt = "[K+].[O-]C(=O)[O-].[K+]"
    reaction = f"COc1ccc(Br)cc1.OB(O)c1ccccc1>{salt}.[Pd]>COc1ccc(-c2ccccc2)cc1"
    assert _roles_of(reaction, [salt])[salt] == roles.BASE
    # And a slot holding only half of a complex does not claim it.
    partial = "Brc1ccccc1.[Fe+2]>[Pd]>c1ccccc1"
    assert _roles_of(partial, ["[Fe+2].c1ccc(P(c2ccccc2)[c-]2cccc2)cc1"]) == {
        "[Fe+2].c1ccc(P(c2ccccc2)[c-]2cccc2)cc1": roles.UNKNOWN
    }
