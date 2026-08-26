"""What the tools answer, and — the half that matters more — what they refuse.

No transport is imported here: these call the engine the way `tools.py` does, so the chemistry is
proven without a server running. `test_server.py` is where the wire is tested.

Three refusals carry most of the value in this file, and each of them was a real defect somewhere
before it was a rule:

- an **unknown name resolves to nothing**, never to a fabricated structure;
- an **unresolvable solvent is an error**, not a dropped row, because a missing solvent leaves a
  table that looks complete while halving every mass metric derived from it;
- a **mass balance that does not close is refused**, because a negative E-factor reads as an
  implausibly green process rather than as the data error it is.
"""

from __future__ import annotations

import math

import pytest
from chemclaw_mcp_chem.engine.chem import InvalidSmilesError, molecular_weight
from chemclaw_mcp_chem.engine.depiction import RENDER_SIZE_PX, render_svg
from chemclaw_mcp_chem.engine.reagents import density_of, resolve_compound_name
from chemclaw_mcp_chem.engine.stoichiometry import charge_table, green_metrics


class TestResolveCompound:
    """Names, abbreviations and structures all arrive at one canonical answer, or at none."""

    @pytest.mark.parametrize(
        ("written", "expected"),
        [
            ("DIPEA", "CCN(C(C)C)C(C)C"),
            ("dipea", "CCN(C(C)C)C(C)C"),
            ("Hunig's base", "CCN(C(C)C)C(C)C"),
            ("2-MeTHF", "CC1CCCO1"),
            ("2 MeTHF", "CC1CCCO1"),
            ("N,N-dimethylformamide", "CN(C)C=O"),
        ],
    )
    def test_a_spelling_a_chemist_would_type_resolves(self, written: str, expected: str) -> None:
        """Case, spaces, hyphens and apostrophes stop mattering; the structure does not."""
        match = resolve_compound_name(written)
        assert match is not None
        assert match.smiles == expected
        assert match.source == "synonym"
        assert match.query == written

    def test_a_structure_resolves_to_itself_and_picks_up_the_name(self) -> None:
        """A caller who already holds a SMILES gets the canonical form and the recognised name."""
        match = resolve_compound_name("OCC")
        assert match is not None
        assert (match.smiles, match.name, match.source) == ("CCO", "ethanol", "smiles")

    def test_an_unrecognised_structure_keeps_the_query_as_its_name(self) -> None:
        """Not every valid molecule is in the table; the structure is still the honest answer."""
        match = resolve_compound_name("CCCCCCCCCCCCCCCCCC(=O)O")
        assert match is not None
        assert match.source == "smiles"
        assert match.name == "CCCCCCCCCCCCCCCCCC(=O)O"

    @pytest.mark.parametrize("written", ["unobtainium", "the usual base", "CCO junk", ""])
    def test_an_unknown_name_resolves_to_nothing(self, written: str) -> None:
        """`None` is a real answer. A guessed structure corrupts everything downstream of it."""
        assert resolve_compound_name(written) is None

    @pytest.mark.parametrize(
        ("written", "formula_reading", "smiles_reading"),
        [
            ("CO", "carbon monoxide", "methanol"),
            ("NO", "nitric oxide", "hydroxylamine"),
            ("CN", "cyanide", "methylamine"),
            ("S", "sulfur", "hydrogen sulfide"),
            ("B", "boron", "borane"),
            ("O", "oxygen", "water"),
        ],
    )
    def test_a_formula_that_is_also_a_smiles_is_refused_by_name(
        self, written: str, formula_reading: str, smiles_reading: str
    ) -> None:
        """`CO` is what a chemist writes for a gas and what RDKit reads as methanol.

        Measured before this refusal existed: `stoichiometry_table(basis="Brc1ccccc1",
        basis_mass_g=100, reagents=["CO"], equivalents=[1.5])` returned a complete table with an
        empty `unresolved`, naming methanol at MW 32.042 and instructing 30.61 g of a liquid to be
        weighed out for a gas. Carbon monoxide is 28.010.
        """
        with pytest.raises(ValueError, match=formula_reading) as refusal:
            resolve_compound_name(written)
        assert smiles_reading in str(refusal.value), (
            f"in: {written!r}  out: {refusal.value} — the refusal must name both readings, "
            "since the caller is the one who knows which was meant"
        )

    def test_the_ambiguity_does_not_reach_a_charge_table(self) -> None:
        """The failure this refusal exists for, asserted where the mass would have been printed."""
        with pytest.raises(ValueError, match="carbon monoxide"):
            charge_table("Brc1ccccc1", 100.0, ["CO"], [1.5], [], [])

    def test_a_curated_spelling_still_wins(self) -> None:
        """The table decides before the ambiguity does, so a reviewed name is never refused."""
        match = resolve_compound_name("MeOH")
        assert match is not None
        assert (match.smiles, match.name, match.source) == ("CO", "methanol", "synonym")


class TestDensity:
    """A density is a fact about a substance, and its absence is load-bearing."""

    def test_every_spelling_of_one_solvent_agrees(self) -> None:
        """`THF`, `tetrahydrofuran` and the structure are one substance, so one number."""
        assert density_of("THF") == density_of("tetrahydrofuran") == density_of("C1CCOC1") == 0.889

    def test_a_reagent_that_is_not_charged_by_volume_has_none(self) -> None:
        """`None` here means "not on file", which is why the caller must refuse to convert."""
        assert density_of("HATU") is None

    def test_an_unknown_name_has_none(self) -> None:
        """The other meaning of `None`, and the caller must treat both the same way."""
        assert density_of("unobtainium") is None


class TestChargeTable:
    """The everyday bench question, and the arithmetic behind it."""

    @pytest.mark.parametrize(
        ("smiles", "expected"),
        [("CC(=O)O", 60.05), ("C1CCOC1", 72.11), ("ClCCl", 84.93)],
    )
    def test_the_molecular_weight_is_the_average_one(self, smiles: str, expected: float) -> None:
        """Literal weights, because every mass in a charge table is one of these times a number.

        Also the coverage behind the one `type: ignore` in `engine/chem.py`: `rdkit-stubs` omits
        `Descriptors.MolWt`, so this is what says the ignored call still returns the right number —
        and the *average* one, which is why dichloromethane is here at 84.93 rather than 83.95.
        """
        assert round(molecular_weight(smiles), 2) == expected

    def test_a_reagent_is_scaled_by_its_equivalents(self) -> None:
        """1.2 equiv of triethylamine on a 100 g acetic acid basis, in grams."""
        table = charge_table("AcOH", 100.0, ["TEA"], [1.2], [], [])
        basis, reagent = table.rows
        assert (table.basis_name, basis.role, basis.mass_g) == ("acetic acid", "basis", 100.0)
        assert math.isclose(basis.moles_mmol, 100.0 / molecular_weight("CC(=O)O") * 1000.0)
        assert reagent.name == "triethylamine"
        assert reagent.role == "reagent"
        assert math.isclose(reagent.moles_mmol, basis.moles_mmol * 1.2)
        assert math.isclose(reagent.mass_g, reagent.moles_mmol * reagent.molecular_weight / 1000.0)

    def test_a_solvent_is_charged_in_volumes_and_comes_back_as_a_mass(self) -> None:
        """10 volumes of THF on a 100 g basis is 1000 mL, and 889 g of it.

        This is the case the two-path argument exists for: expressed as molar equivalents instead,
        the same charge came out 2.17x wrong on a live 2 kg run.
        """
        table = charge_table("AcOH", 100.0, [], [], ["THF"], [10.0])
        solvent = table.rows[1]
        assert solvent.role == "solvent"
        assert solvent.volume_ml == 1000.0
        assert solvent.density_g_per_ml == 0.889
        assert math.isclose(solvent.mass_g, 889.0)
        assert math.isclose(solvent.moles_mmol, 889.0 / solvent.molecular_weight * 1000.0)

    def test_an_unresolvable_reagent_is_listed_rather_than_guessed(self) -> None:
        """A chemist reads a charge list line by line and sees the missing row."""
        table = charge_table("AcOH", 100.0, ["unobtainium", "TEA"], [1.0, 1.2], [], [])
        assert table.unresolved == ["unobtainium"]
        assert [row.name for row in table.rows] == ["acetic acid", "triethylamine"]

    def test_an_unresolvable_solvent_is_an_error(self) -> None:
        """The asymmetry with a reagent is deliberate: a dropped solvent looks like a full table."""
        with pytest.raises(ValueError, match="could not resolve the solvent"):
            charge_table("AcOH", 100.0, [], [], ["unobtainium"], [10.0])

    def test_a_solvent_with_no_density_is_an_error_that_says_what_to_do(self) -> None:
        """Neither a zero nor a guessed 1 g/mL is an acceptable stand-in for a real density."""
        with pytest.raises(ValueError, match="no density on file"):
            charge_table("AcOH", 100.0, [], [], ["pyridine"], [5.0])

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"reagents": ["TEA"], "equivalents": []}, "they must match"),
            ({"solvents": ["THF"], "volumes": []}, "they must match"),
            ({"basis_mass_g": 0.0}, "basis_mass_g must be positive"),
            ({"solvents": ["THF"], "volumes": [0.0]}, "must be positive"),
        ],
    )
    def test_an_incoherent_charge_is_refused(self, kwargs: dict[str, object], message: str) -> None:
        """Paired lists and positive quantities, checked before any chemistry happens."""
        call: dict[str, object] = {
            "basis": "AcOH",
            "basis_mass_g": 100.0,
            "reagents": [],
            "equivalents": [],
            "solvents": [],
            "volumes": [],
        }
        call.update(kwargs)
        with pytest.raises(ValueError, match=message):
            charge_table(**call)  # type: ignore[arg-type]

    def test_an_unresolvable_basis_is_an_error(self) -> None:
        """Nothing can be scaled to a basis that does not exist."""
        with pytest.raises(ValueError, match="could not resolve the limiting reagent"):
            charge_table("unobtainium", 100.0, [], [], [], [])


class TestGreenMetrics:
    """E-factor and PMI, and the mass balance they are only meaningful inside."""

    def test_the_two_metrics_differ_by_exactly_one(self) -> None:
        """True by construction, which makes it the cheapest possible check on the arithmetic."""
        metrics = green_metrics([100.0, 50.0, 889.0], 80.0)
        assert math.isclose(metrics.pmi - metrics.e_factor, 1.0)
        assert math.isclose(metrics.total_input_kg, 1.039)
        assert math.isclose(metrics.waste_kg, 1.039 - 0.08)
        assert math.isclose(metrics.e_factor, (1039.0 - 80.0) / 80.0)

    def test_leaving_the_solvent_out_is_visible_in_the_number(self) -> None:
        """Not a refusal — the tool cannot know — but the reason the docstring insists on it."""
        with_solvent = green_metrics([100.0, 50.0, 889.0], 80.0)
        without = green_metrics([100.0, 50.0], 80.0)
        assert without.e_factor < with_solvent.e_factor / 5

    @pytest.mark.parametrize(
        ("masses", "product", "message"),
        [
            ([100.0], 0.0, "product_mass_g must be positive"),
            ([100.0, -1.0], 10.0, "must not be negative"),
            ([10.0], 80.0, "mass balance is unsound"),
        ],
    )
    def test_an_unsound_balance_is_refused(
        self, masses: list[float], product: float, message: str
    ) -> None:
        """A negative E-factor reads as an implausibly green process, so it is never returned."""
        with pytest.raises(ValueError, match=message):
            green_metrics(masses, product)


class TestDepiction:
    """A picture, and the strings that are not one."""

    def test_a_molecule_is_drawn_at_the_configured_size(self) -> None:
        """An SVG document with atoms in it, sized by the one knob this server has."""
        svg = render_svg("CC(=O)Oc1ccccc1C(=O)O")
        assert svg.startswith("<?xml")
        assert "<svg" in svg
        assert f"width='{RENDER_SIZE_PX}px'" in svg

    def test_a_reaction_is_drawn_twice_as_wide(self) -> None:
        """A reaction needs room for both sides and the arrow between them."""
        svg = render_svg("CCO>>CC=O")
        assert f"width='{RENDER_SIZE_PX * 2}px'" in svg

    @pytest.mark.parametrize("written", ["CCO junk", "", "not-a-molecule"])
    def test_an_undrawable_molecule_is_refused(self, written: str) -> None:
        """The same strict gate as everywhere else: a truncated parse draws the wrong molecule."""
        with pytest.raises(InvalidSmilesError):
            render_svg(written)

    @pytest.mark.parametrize(
        "written",
        [
            "not-a-molecule>>also-not",  # RDKit raises its own parser exception; retranslated
            "CCO junk>>CC=O",  # the truncation case, which RDKit refuses outright for reactions
            ">>",  # parses to an empty reaction and would draw a blank picture
            "°C>>CC=O",  # parses — as *methane* — because RDKit skips non-ASCII at an edge
        ],
    )
    def test_an_undrawable_reaction_is_refused(self, written: str) -> None:
        """Refused as a reaction, because the `>>` said that is what it was meant to be."""
        with pytest.raises(InvalidSmilesError, match="reaction"):
            render_svg(written)


class TestAQuantityIsPositiveOrItIsRefused:
    """`charge_table`'s own `Raises` clause promises a `ValueError` when a quantity is not positive.

    `basis_mass_g` and every volume were checked; `equivalents` was not, and an equivalent count is
    a quantity. Measured: `stoichiometry_table("toluene", 100, ["triethylamine"], [-2.0])` returned
    a complete table whose reagent row read **-219.65 g** at -2170.6 mmol — a charge list that
    reads as authoritative. The one guard that would have caught it downstream reports it as a
    mass-balance problem rather than as the bad input.
    """

    @pytest.mark.parametrize("equivalents", [[-2.0], [0.0], [1.2, -0.1]])
    def test_a_non_positive_equivalent_count_is_refused(self, equivalents: list[float]) -> None:
        reagents = ["triethylamine"] * len(equivalents)
        with pytest.raises(ValueError, match="equivalents"):
            charge_table("toluene", 100.0, reagents, equivalents, [], [])


class TestAChargeRowSaysWhereItsNumbersCameFrom:
    """ "Every result carries `source`" is this fleet's rule, and the charge table dropped it.

    A charge list is pasted into a batch record, and "THF, 0.889 g/mL" with no attribution leaves
    the reader unable to tell a curated table value from a name that resolved only because the
    string happened to parse as a SMILES.
    """

    def test_a_named_reagent_and_a_typed_structure_are_told_apart(self) -> None:
        table = charge_table("toluene", 100.0, ["CCCCCCCCCCCCCCCCCC(=O)O"], [1.0], ["THF"], [5.0])
        by_role = {row.role: row for row in table.rows}
        assert by_role["basis"].source == "synonym"
        assert by_role["reagent"].source == "smiles", (
            f"in: a typed structure  out: source={by_role['reagent'].source!r}"
        )
        assert by_role["solvent"].source == "synonym"
        assert by_role["solvent"].density_source == "bench-reagents"


class TestAReactionDrawingIsTheWholeReaction:
    """The molecule path refuses embedded whitespace; the reaction path branched before that check.

    `_reaction`'s own docstring records that `"CCO junk>>CC=O"` raises — measured, and true, because
    RDKit rejects a *reactant* it cannot read. What was never measured is whitespace in the **last**
    component, which it truncates instead: `"CCO>>CC=O CCCCCCBr"` drew `CCO >> CC=O`, a well-formed
    and plausible picture of a different reaction, and a drawing is the one form in which the
    model's choice is supposed to become checkable by a human.
    """

    @pytest.mark.parametrize(
        "written",
        ["CCO>>CC=O CCCCCCBr", "CCO>>CC=O junk", "CCO junk>>CC=O", "CCO>>CC=O\tCCCCCCBr"],
    )
    def test_a_truncating_reaction_string_is_refused_rather_than_drawn(self, written: str) -> None:
        with pytest.raises(InvalidSmilesError):
            render_svg(written)

    def test_a_reaction_written_properly_still_draws(self) -> None:
        """Components are separated by a dot, and that one must keep working."""
        assert render_svg("CCO>>CC=O.CCCCCCBr").startswith("<?xml")
