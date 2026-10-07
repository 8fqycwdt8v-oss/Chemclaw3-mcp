"""The role rules, on reactions a chemist would recognise.

Each test names its chemistry claim. The context-dependent pair (a phosphine is a ligand or a
reagent depending on the rest of the flask) is why this operates on a reaction rather than a
molecule. These run without the optional models: this is what RDKit alone gives.
"""

from __future__ import annotations

import pytest
from chemclaw_mcp_rxnlabel.engine import agents, mapping, roles, species

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
    """The assigned role of each structure, keyed by the structure.

    Maps the reaction the way the tool surface does — once, and passes the result in — so these
    tests exercise the call `_represent` actually makes.
    """
    mapped = mapping.map_reaction(reaction).mapped
    return dict(zip(structures, roles.assign(reaction, structures, mapped), strict=True))


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
    """The same phosphine is a ligand with a metal and not without one.

    Triphenylphosphine is a ligand in a Suzuki and a reagent in a Mitsunobu; only the rest of the
    flask distinguishes them, which is why `assign` takes a reaction.
    """
    ppd = "c1ccc(P(c2ccccc2)c2ccccc2)cc1"
    assert _roles_of(SUZUKI, [ppd])[ppd] == roles.LIGAND
    assert _roles_of(MITSUNOBU, [ppd])[ppd] != roles.LIGAND


def test_a_ferrocenyl_phosphine_is_a_ligand_and_not_a_catalyst() -> None:
    """dppf contains iron and is a ligand, so the ligand rule is consulted before the metal one.

    "Contains a transition metal, therefore catalyst" is right for Pd(OAc)2 and wrong for every
    ferrocene-backboned ligand.
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
    """Roles are matched by structure, not position: the answer is invariant under shuffling.

    The caller's record orders species differently from the reaction string, which groups agents in
    the middle, so a positional match would mislabel every reaction with a solvent.
    """
    structures = ["CC#N", "c1ccc(NC2CCCCC2)cc1", "CC(=O)O[Pd]OC(C)=O", "Brc1ccccc1"]
    forwards = _roles_of(BUCHWALD, structures)
    backwards = _roles_of(BUCHWALD, list(reversed(structures)))
    assert forwards == backwards


def test_an_unreadable_reaction_yields_unknown_for_everything() -> None:
    """A malformed record labels nothing rather than labelling it wrongly."""
    assert roles.assign("not a reaction", ["CCO"], None) == [roles.UNKNOWN]


@pytest.mark.parametrize(("name", "smarts"), species.FUNCTIONAL_GROUPS)
def test_every_functional_group_pattern_compiles(name: str, smarts: str) -> None:
    """Each pattern individually, because `_matches_any` skips one that does not compile.

    That leniency is right at runtime — one bad pattern must not fail every classification — and it
    means a typo would silently narrow the vocabulary. This is where it is caught instead.
    """
    from rdkit import Chem

    assert Chem.MolFromSmarts(smarts) is not None, f"{name} has an uncompilable SMARTS"


def test_a_multi_component_species_is_matched_component_wise() -> None:
    """A multi-component species (salt, complex) is matched component-wise.

    A species belongs to a slot when every dot-separated component is written there, which reduces
    to plain membership for a single-component species.
    """
    salt = "[K+].[O-]C(=O)[O-].[K+]"
    reaction = f"COc1ccc(Br)cc1.OB(O)c1ccccc1>{salt}.[Pd]>COc1ccc(-c2ccccc2)cc1"
    assert _roles_of(reaction, [salt])[salt] == roles.BASE
    # And a slot holding only half of a complex does not claim it.
    partial = "Brc1ccccc1.[Fe+2]>[Pd]>c1ccccc1"
    assert _roles_of(partial, ["[Fe+2].c1ccc(P(c2ccccc2)[c-]2cccc2)cc1"]) == {
        "[Fe+2].c1ccc(P(c2ccccc2)[c-]2cccc2)cc1": roles.UNKNOWN
    }


class TestABaseIsRecognisedByAPatternThatMatchesIt:
    """A base is recognised by a pattern that actually matches it.

    A misclassified base is a wrong count in a frequency table. Bicarbonate's anionic oxygen has one
    connection, so the pattern must not demand two; aromatic-nitrogen bases need a rule of their
    own.
    """

    @pytest.mark.parametrize(
        ("name", "smiles"),
        [
            ("sodium bicarbonate", "OC(=O)[O-]"),
            ("bicarbonate, written anion-first", "[O-]C(=O)O"),
            ("dipotassium hydrogenphosphate", "O=P([O-])([O-])O"),
            ("pyridine", "c1ccncc1"),
            ("2,6-lutidine", "Cc1cccc(C)n1"),
            ("collidine", "Cc1cc(C)nc(C)c1"),
            ("N-methylimidazole", "Cn1ccnc1"),
            ("imidazole", "c1c[nH]cn1"),
            ("DMAP", "CN(C)c1ccncc1"),
            # Kept from the rules that already worked, so a widened pattern cannot lose them.
            ("sodium carbonate", "[O-]C(=O)[O-]"),
            ("caesium fluoride", "[F-]"),
            ("potassium tert-butoxide", "CC(C)(C)[O-]"),
            ("triethylamine", "CCN(CC)CC"),
        ],
    )
    def test_a_base_a_process_chemist_charges_is_classified_as_one(
        self, name: str, smiles: str
    ) -> None:
        assert agents.is_base(smiles), f"in: {smiles} ({name})  out: is_base=False"

    @pytest.mark.parametrize(
        ("name", "smiles"),
        [
            ("HOBt — an additive, and mildly acidic", "On1nnc2ccccc21"),
            ("1,2,4-triazole", "c1nc[nH]n1"),
            ("tetrazole", "c1nn[nH]n1"),
            ("monopotassium phosphate — a buffer, not a base", "O=P([O-])(O)O"),
            ("benzoic acid", "O=C(O)c1ccccc1"),
        ],
    )
    def test_what_is_not_a_base_is_still_not_one(self, name: str, smiles: str) -> None:
        """The widened rules must not sweep in the acidic azoles that sit in the same slot."""
        assert not agents.is_base(smiles), f"in: {smiles} ({name})  out: is_base=True"

    def test_a_bicarbonate_suzuki_names_its_base(self) -> None:
        """End to end, on the commonest base in the corpus this server was built to label."""
        reaction = (
            "COc1ccc(Br)cc1.OB(O)c1ccccc1"
            ">c1ccc(P(c2ccccc2)c2ccccc2)cc1.OC(=O)[O-].C1CCOC1.[Pd]"
            ">COc1ccc(-c2ccccc2)cc1"
        )
        assigned = _roles_of(reaction, ["OC(=O)[O-]"])
        assert assigned["OC(=O)[O-]"] == roles.BASE, f"in: OC(=O)[O-]  out: {assigned}"


class TestASpeciesIsParsedWholeOrNotAtAll:
    """A species is parsed whole or not at all.

    RDKit treats whitespace as the end of a SMILES, so `"CCO junk"` would silently narrow to ethanol
    and be stored as the label for a concatenated ELN cell.
    """

    @pytest.mark.parametrize(
        "written", ["CCO junk", "CCO (2 vol)", "CCO\t50 mL", " ", "", "°C", "CCO 2"]
    )
    def test_a_string_rdkit_would_truncate_is_not_read(self, written: str) -> None:
        assert species.canonical_smiles(written) is None, (
            f"in: {written!r}  out: {species.canonical_smiles(written)!r} — a molecule nobody sent"
        )
        assert species.functional_groups(written) is None, (
            f"in: {written!r}  out: {species.functional_groups(written)!r}"
        )
        assert species.scaffold(written) is None

    def test_an_unreadable_species_is_told_apart_from_one_that_carries_no_group(self) -> None:
        """`[]` meant both "read it, no groups" and "could not read it", and the two are stored
        the same way — so every "which products carry an aryl halide" query counted an unlabelled
        row as a negative rather than as unknown.
        """
        assert species.functional_groups("CC") == []
        assert species.functional_groups("$$bogus$$") is None

    def test_a_surrounding_newline_is_still_a_copy_paste_artefact(self) -> None:
        """Stripped rather than refused, the same call `chem.require_molecule` makes."""
        assert species.canonical_smiles("\n CCO \n") == "CCO"
