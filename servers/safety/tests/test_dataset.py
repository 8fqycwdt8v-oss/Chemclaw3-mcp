"""The four corpora validated against themselves, and the provenance a reviewer signed off on.

Each table gets the strongest self-check it supports: `ich_q3c.yaml`, the guideline's
`ppm = PDE x 100` identity over every Class 2 row; `ich_q3d.yaml`, oral >= parenteral on every
element (inhalation is deliberately not ordered); `rules.yaml` and `genotox_alerts.yaml`,
structural checks (SMARTS compile, unique ids, pair arms equal to their structural twins).
Each `dataset.json`'s claims are checked too, including the deliberate omissions, which must not
be "completed" from memory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from chemclaw_mcp_safety.engine import genotox, ich, reagents, screen
from mcp_server_kit import load_dataset

# One loader for all four, called by its own name rather than through whichever module happens to
# have imported it — `read_table` living in `screen.py` is a fact about the engine's layout, not
# about the alert table or the ICH tables.
RULES = screen.read_table(screen.RULES_DIR, screen.RULES_FILE, screen.RuleTable)
ALERTS = screen.read_table(genotox.ALERTS_DIR, genotox.ALERTS_FILE, genotox.AlertTable)
Q3C = screen.read_table(ich.Q3C_DIR, ich.Q3C_FILE, ich.Q3cTable)
Q3D = screen.read_table(ich.Q3D_DIR, ich.Q3D_FILE, ich.Q3dTable)

CORPORA = [
    (screen.RULES_DIR, screen.RULES_FILE),
    (genotox.ALERTS_DIR, genotox.ALERTS_FILE),
    (ich.Q3C_DIR, ich.Q3C_FILE),
    (ich.Q3D_DIR, ich.Q3D_FILE),
]


# --- the manifests ---------------------------------------------------------------------


@pytest.mark.parametrize(("directory", "records_file"), CORPORA, ids=lambda arg: str(arg))
def test_every_corpus_is_the_one_that_was_approved(directory: Path, records_file: str) -> None:
    """`load_dataset` verifies the checksum on load; this asserts that it does.

    A truncated rule table reports "no rule matched", indistinguishable from a clean molecule.
    """
    loaded = load_dataset(directory, records_file=records_file)
    assert loaded.records_path.is_file()
    assert loaded.licence.strip() and loaded.retrieved_from.strip()
    assert loaded.description.strip()


def test_the_hazard_rules_licence_names_its_basis_rather_than_asserting_an_identifier() -> None:
    """The hazard rules' licence names its basis rather than only asserting an identifier.

    The table is first-party: it cites its sources and reproduces none of them (the cited works are
    prose without SMARTS, and the patterns were written here), the same basis as `genotox` and
    unlike the ICH tables, which carry their guideline's terms. The assertion is on the reasoning,
    not on the identifier string, which anyone could type to satisfy the loader.
    """
    licence = load_dataset(screen.RULES_DIR, records_file=screen.RULES_FILE).licence
    assert licence.startswith("CC0-1.0"), licence
    assert "first-party" in licence, (
        "the licence must record why this table is first-party, not merely which licence it "
        "carries — a bare identifier is unreviewable and is what the UNRESOLVED guard existed for"
    )
    assert "genotox" in licence, (
        "the licence must name the sibling corpus it shares a basis with, so the three-way "
        "provenance split in this server stays legible rather than looking arbitrary"
    )


def test_the_ich_manifests_carry_the_caveats_the_files_record() -> None:
    """The ICH manifests carry the caveats the files record.

    Omissions and an unverified field change what an answer means, and `dataset.json` is what a
    reviewer reads. Pinned as strings so a tidying pass cannot shorten them away.
    """
    q3c = load_dataset(ich.Q3C_DIR, records_file=ich.Q3C_FILE).description
    assert "R9 / 2024" in q3c and "nobody has verified" in q3c
    assert "tert-Butyl alcohol is DELIBERATELY OMITTED" in q3c
    q3d = load_dataset(ich.Q3D_DIR, records_file=ich.Q3D_FILE).description
    assert "Ag, Au and Ni are ABSENT" in q3d


# --- the two SMARTS tables -------------------------------------------------------------


def test_every_hazard_rule_compiles_and_is_completely_described() -> None:
    """A rule that does not compile, or cannot be traced, is a rule nobody can act on.

    Compilation is checked here as well as at load because the load is lazy: a broken pattern would
    otherwise first be noticed by a chemist whose screen raised instead of answering.
    """
    for rule in RULES.structural:
        assert screen.compile_smarts(rule.smarts, rule.id) is not None
        assert rule.explanation.strip() and rule.citation.strip()
    for pair in RULES.incompatible_pairs:
        assert screen.compile_smarts(pair.left, pair.id) is not None
        assert screen.compile_smarts(pair.right, pair.id) is not None
        assert pair.explanation.strip() and pair.citation.strip()


def test_no_two_rules_share_an_id() -> None:
    """The pattern map is keyed by id, so a duplicate would silently screen with one of the two."""
    ids = [rule.id for rule in RULES.structural] + [p.id for p in RULES.incompatible_pairs]
    assert len(ids) == len(set(ids)), sorted(ids)


def test_the_hydrazine_pair_arm_is_its_structural_twin_verbatim() -> None:
    """The hydrazine pair arm is the same string as its structural twin.

    Widening one pattern and not the other leaves a screen that reads clean; pinning molecules
    cannot anticipate the next divergence, pinning the patterns equal can. `left` keeps its own
    spelling: an oxidiser arm is a union over four structural rules.
    """
    structural = next(r.smarts for r in RULES.structural if r.id == "hydrazine")
    pair = next(r.right for r in RULES.incompatible_pairs if r.id == "oxidizer-with-reductant")
    assert f"$({structural})" in pair, (
        "the `oxidizer-with-reductant` right arm must embed the `hydrazine` rule's SMARTS "
        f"verbatim; structural={structural!r} right={pair!r}"
    )


def test_the_peroxide_pair_arm_is_its_structural_twin_verbatim_too() -> None:
    """The peroxide pair arm is its structural twin verbatim too.

    Otherwise an anionic peroxide salt plus a complex hydride, the case the pair rule is named for,
    would raise only the structural rule.
    """
    structural = next(r.smarts for r in RULES.structural if r.id == "peroxide")
    left = next(r.left for r in RULES.incompatible_pairs if r.id == "oxidizer-with-reductant")
    assert f"$({structural})" in left


def test_every_genotoxicity_alert_compiles_and_cites_a_published_set() -> None:
    """Every genotoxicity alert compiles and cites one of the manifest's published sets.

    An alert set is what makes a motif an alert rather than an opinion; a row citing anything else
    has left the table's scope.
    """
    sources = ("Ashby", "Benigni", "ICH M7", "EMA")
    rows: list[tuple[str, str]] = [(a.id, a.citation) for a in ALERTS.structural]
    rows += [(p.id, p.citation) for p in ALERTS.formation_pairs]
    for alert in ALERTS.structural:
        assert screen.compile_smarts(alert.smarts, alert.id) is not None
    for pair in ALERTS.formation_pairs:
        assert screen.compile_smarts(pair.left, pair.id) is not None
        assert screen.compile_smarts(pair.right, pair.id) is not None
    for alert_id, citation in rows:
        assert any(source in citation for source in sources), f"{alert_id}: {citation!r}"


def test_an_alert_has_nowhere_to_put_a_class_a_limit_or_a_purge_factor() -> None:
    """An alert has no field for an ICH M7 class, an acceptable intake or a purge factor.

    The promise that this system produces none of them is structural: there is nowhere to put one.
    `GenotoxAlert` has no `severity` either, since ranking alerts would start a classification the
    published sets do not make.
    """
    assert set(genotox.GenotoxAlert.model_fields) == {
        "alert_id",
        "motif",
        "explanation",
        "citation",
        "matched",
    }


def test_no_alert_quotes_a_number_with_a_unit() -> None:
    """No alert explanation quotes a number with a dose or concentration unit.

    A figure in µg/day, mg/day or ppm would be an acceptable intake under a published alert set's
    citation, the one output this table must not produce.
    """
    units = ("µg/day", "mg/day", "ppm")
    texts = [a.explanation for a in ALERTS.structural] + [
        p.explanation for p in ALERTS.formation_pairs
    ]
    for text in texts:
        assert not any(unit in text for unit in units), text


# --- the transcribed ICH tables --------------------------------------------------------


def test_every_class_2_concentration_limit_agrees_with_its_pde() -> None:
    """Every Class 2 row satisfies ppm = PDE x 100 (10 g daily dose).

    Catches a transposed PDE or ppm without the source open. Class 2 only: for Class 3 both numbers
    come from one general statement, so the identity is tautological and would inflate coverage.
    """
    class_2 = [s for s in Q3C.solvents if s.solvent_class == "2"]
    assert len(class_2) == 32, "the Class 2 block is the bulk of Q3C; this is checking the table"
    for solvent in class_2:
        assert solvent.pde_mg_per_day is not None, solvent.name
        assert solvent.concentration_limit_ppm == pytest.approx(solvent.pde_mg_per_day * 100.0), (
            solvent.name
        )


def test_class_1_is_quoted_as_a_concentration_and_class_3_as_the_general_statement() -> None:
    """Class 1 is quoted as a concentration and Class 3 as the general statement.

    Q3C assigns Class 1 no PDE and Class 3 no solvent-specific PDE; a row carrying one would invent
    a number under a real citation.
    """
    for solvent in Q3C.solvents:
        if solvent.solvent_class == "1":
            assert solvent.pde_mg_per_day is None, solvent.name
        if solvent.solvent_class == "3":
            assert (solvent.pde_mg_per_day, solvent.concentration_limit_ppm) == (50.0, 5000.0), (
                solvent.name
            )
    class_3 = Q3C.classes["3"]
    assert "no solvent-specific PDE" in class_3.pde_basis
    assert "general limit" in class_3.pde_basis


def test_every_row_names_a_class_the_file_defines() -> None:
    """A row pointing at a class note that does not exist would fail at lookup, not at load."""
    for solvent in Q3C.solvents:
        assert solvent.solvent_class in Q3C.classes
    for element in Q3D.elements:
        assert element.element_class in Q3D.classes


def test_every_elemental_pde_is_positive_and_oral_is_never_below_parenteral() -> None:
    """Every elemental PDE is positive and oral is never below parenteral.

    Not a full ordering: cadmium and selenium quote an inhalation PDE above parenteral, so asserting
    one would be "fixed" by editing true numbers.
    """
    for element in Q3D.elements:
        pdes = (
            element.oral_pde_ug_per_day,
            element.parenteral_pde_ug_per_day,
            element.inhalation_pde_ug_per_day,
        )
        assert all(value > 0 for value in pdes), element.symbol
        assert element.oral_pde_ug_per_day >= element.parenteral_pde_ug_per_day, element.symbol


def test_no_spelling_answers_to_two_substances() -> None:
    """No spelling answers to two substances, across both ICH files together.

    A collision hands a reader another substance's limit with a real citation. `_register` refuses
    one per file; this checks the public surface across both.
    """
    for query, limit in ich.index().items():
        assert ich.impurity_limit(query).limit is limit


@pytest.mark.parametrize(
    "absent",
    ["tert-butyl alcohol", "tert-butanol", "water", "nickel", "Ni", "silver", "Ag", "gold", "Au"],
)
def test_a_deliberate_omission_is_still_omitted(absent: str) -> None:
    """The deliberate omissions stay omitted.

    Each is a real substance left out because its value could not be verified (or, for water, Q3C
    does not cover it); the miss verdict already tells the reader to consult the guideline.
    """
    assert ich.impurity_limit(absent).limit is None


@pytest.mark.parametrize("ambiguous", ["EDC", "DMA", "TCE"])
def test_an_ambiguous_abbreviation_names_nothing_here(ambiguous: str) -> None:
    """An ambiguous abbreviation resolves to nothing.

    `EDC` is ethylene dichloride in Q3C and a carbodiimide coupling reagent at the bench; resolving
    it would hand a chemist a Class 1 limit with a genuine citation for the wrong substance.
    """
    assert ich.impurity_limit(ambiguous).limit is None


# --- the vendored reagent table --------------------------------------------------------


def test_the_reagent_corpus_is_byte_identical_to_the_one_chem_ships() -> None:
    """This server's reagent table is byte-identical to the one `chem` ships.

    Servers never import each other, so a shared table is carried twice, and identical bytes are
    what make that safe. `tests/test_fleet_*.py` asserts the same from outside. A deliberate
    divergence needs an argument written here.
    """
    here = reagents.DATA_DIR
    there = here.parents[4] / "chem" / "src" / "chemclaw_mcp_chem" / "data"
    assert (here / "records.csv").read_bytes() == (there / "records.csv").read_bytes()
    assert json.loads((here / "dataset.json").read_text(encoding="utf-8")) == json.loads(
        (there / "dataset.json").read_text(encoding="utf-8")
    )


def test_the_reagent_table_is_what_makes_a_structure_reach_an_ich_row() -> None:
    """The reagent table is what lets a structure reach an ICH row.

    A SMILES is not a name, so without it `ich_impurity_limit("C1CCOC1")` would miss a solvent whose
    row exists.
    """
    assert reagents.resolve_compound_name("C1CCOC1") is not None
    assert ich.impurity_limit("C1CCOC1").limit is not None


def test_the_peroxide_ketone_arm_is_deliberately_not_its_structural_twin() -> None:
    """The peroxide-ketone pair arm is deliberately not its structural twin.

    The `peroxide` rule alerts on any O-O bond; this rule needs a source of HO-OH, and a dialkyl
    peroxide plus acetone cannot form acetone peroxide. So twin-equality does not generalise by id,
    and equality here would be a false flag. Each widening instead pins a molecule on each side in
    `tests/test_pairs.py`.
    """
    structural = next(r.smarts for r in RULES.structural if r.id == "peroxide")
    arm = next(r.left for r in RULES.incompatible_pairs if r.id == "peroxide-with-ketone")
    assert arm != structural
    assert "OX2H" in arm, "the arm must still require a hydroxyl-bearing (or salt) peroxide"


def test_the_two_hydride_pair_rules_recognise_the_same_reagent_class() -> None:
    """The two hydride pair rules recognise the same reagent class with the same string.

    They differ only in the solvent arm; a widening applied to one alone would flag NaH in DMSO and
    clear it in dichloromethane. Not covered by the id-derived check, since neither is a structural
    rule.
    """
    arms = {rule.id: rule.left for rule in RULES.incompatible_pairs}
    assert arms["saline-hydride-with-chlorinated-solvent"] == arms["hydride-with-dipolar-aprotic"]
