"""The four corpora validated against themselves, and the provenance a reviewer signed off on.

`props` sets the bar: check pairs of *independently written* numbers against each other, so a
transposed digit fails a test instead of answering a question. Each table here has a different
version of that, and the differences are the point:

- **`ich_q3c.yaml` has the strongest one available anywhere in this server.** The guideline quotes
  every Class 2 solvent twice — a PDE in mg/day and a concentration limit in ppm — and its own
  Option 1 relation between them is `ppm = PDE x 100` at a 10 g/day maximum daily dose. Two numbers
  transcribed separately from two columns, related by a rule neither of them carries: that is
  exactly the check a hand transcription needs, and it is run over all 32 Class 2 rows.
- **`ich_q3d.yaml` has a weaker but real one:** oral >= parenteral on every element, which a
  transposed pair of columns breaks. The inhalation column is deliberately *not* in that ordering —
  Cd and Se both quote an inhalation PDE above their parenteral one — so asserting a full ordering
  would be asserting a fiction.
- **`rules.yaml` and `genotox_alerts.yaml` carry no numbers at all.** Their self-check is
  structural: every SMARTS compiles, every id is unique, and the one invariant the rule table has
  cost two silent failures to learn — the `hydrazine` structural rule and the
  `oxidizer-with-reductant` right arm must be the *same string* — is pinned here rather than in a
  behavioural test, because it is a property of the table.

The rest of this file checks the claims each `dataset.json` makes about itself, including the
**deliberate omissions**. Those are the load-bearing half of a transcription: `tert`-butyl alcohol,
water, and Ag/Au/Ni are absent on purpose, and a future contributor who "completes the table" from
memory would be reintroducing the exact fabrication these files were written to end.
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
    """`load_dataset` verifies the checksum on load; this is the assertion that it is doing so.

    A rule table that was truncated by a bad COPY is a *shorter* rule table, and a shorter rule
    table reports "no rule matched" — indistinguishable from a clean molecule. The checksum is the
    only thing standing between that and a silent screen.
    """
    loaded = load_dataset(directory, records_file=records_file)
    assert loaded.records_path.is_file()
    assert loaded.licence.strip() and loaded.retrieved_from.strip()
    assert loaded.description.strip()


def test_the_hazard_rules_licence_names_its_basis_rather_than_asserting_an_identifier() -> None:
    """The rule table is first-party, and the manifest has to say *why* — not just name a licence.

    It shipped as `UNRESOLVED` through the port, guarded by a test pinning that prefix, because the
    file carries a citation on every rule and no licence statement anywhere. Settled since: it
    cites its sources and reproduces none of them. Bretherick's and the four cited papers are
    prose and contain no SMARTS; the patterns here were written *and debugged* in Chemclaw3, which
    the `peroxide` rule shows plainly — it carries an in-repo fix for `[OX1-]`, because the
    two-coordinate-only pattern had screened sodium peroxide clean. And the underlying facts (an
    organic azide is shock-sensitive) are not copyrightable subject matter.

    That is the same basis as `genotox` and deliberately not the basis `ich_q3c`/`ich_q3d` use —
    those transcribe figures out of a guideline and carry that guideline's terms. **The asymmetry
    was the defect**: three sibling corpora with one provenance question answered three ways, one
    of them left open, which reads to a later maintainer as a real problem with this file rather
    than as caution.

    So the assertion is on the *reasoning*, not on the string `CC0-1.0`. A bare identifier is
    exactly what the original test existed to prevent somebody typing to satisfy the loader, and
    that failure mode does not go away now that the identifier happens to be right.
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
    """A caveat that lives only in a YAML comment is one nobody reading `dataset.json` will see.

    Both ICH tables carry omissions and an unverified field that change what an answer means, and
    `dataset.json` is what a reviewer reads instead of the file. Pinned as strings because the
    realistic regression is a tidying pass that shortens the description.
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
    """The two hydrazine patterns must be the *same string*, not merely both recently widened.

    This rule set has been half-fixed twice — the peroxide widening reached the structural rule
    before its pair arm, and the hydrazine widening did it again with the H requirement, each time
    leaving a screen that reads clean. Pinning individual molecules cannot prevent the third
    occurrence, because the next divergence will be some molecule nobody listed. Pinning the
    patterns equal does: the same motif, screened by the same characters, in both places.

    `left` deliberately keeps its own spelling — an oxidiser arm is a union over four structural
    rules, not a twin of any one of them.
    """
    structural = next(r.smarts for r in RULES.structural if r.id == "hydrazine")
    pair = next(r.right for r in RULES.incompatible_pairs if r.id == "oxidizer-with-reductant")
    assert f"$({structural})" in pair, (
        "the `oxidizer-with-reductant` right arm must embed the `hydrazine` rule's SMARTS "
        f"verbatim; structural={structural!r} right={pair!r}"
    )


def test_the_peroxide_pair_arm_is_its_structural_twin_verbatim_too() -> None:
    """The other half of the same lesson: `Na2O2 + NaBH4` raised only the structural rule.

    The `peroxide` rule was widened to `[OX2,OX1-][OX2,OX1-]` for the anionic peroxide salt and its
    twin in `incompatible_pairs` kept the two-coordinate-only form, so a strong solid oxidiser mixed
    with a complex hydride — the case the pair rule is named for — was the one that did not fire.
    """
    structural = next(r.smarts for r in RULES.structural if r.id == "peroxide")
    left = next(r.left for r in RULES.incompatible_pairs if r.id == "oxidizer-with-reductant")
    assert f"$({structural})" in left


def test_every_genotoxicity_alert_compiles_and_cites_a_published_set() -> None:
    """An alert a chemist cannot trace is an alert they must take on trust — which is the failure.

    The citation is checked against the four sources the manifest records rather than for mere
    non-emptiness: an alert set is what makes a motif an *alert* rather than an opinion, and a row
    citing something else has left the table's stated scope.
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
    """The line the table does not cross, asserted on the payload shape rather than on prose.

    `AlertResult.verdict` says on every result that this system cannot produce an ICH M7 class, an
    acceptable intake or a purge factor. The strongest form of that promise is structural: there is
    no field to put one in. A prose blacklist cannot state it — the `n-nitroso` explanation names a
    purge argument precisely to say it does *not* follow from the flag, which is the sentence the
    table exists to make and exactly what a keyword ban would delete.

    `GenotoxAlert` also has no `severity`, unlike `HazardFlag`, and that is the same decision one
    step further: ranking alerts would be the first half of a classification, and the published
    alert sets do not rank them either.
    """
    assert set(genotox.GenotoxAlert.model_fields) == {
        "alert_id",
        "motif",
        "explanation",
        "citation",
        "matched",
    }


def test_no_alert_quotes_a_number_with_a_unit() -> None:
    """What a keyword ban *can* say: an alert names a motif, it never quotes a limit.

    A figure in µg/day, mg/day or ppm inside an alert explanation would be an acceptable intake
    wearing an alert's clothes — the one output this table is written not to produce — and it would
    ship inside the payload, under the citation of a published alert set that contains no such
    number.
    """
    units = ("µg/day", "mg/day", "ppm")
    texts = [a.explanation for a in ALERTS.structural] + [
        p.explanation for p in ALERTS.formation_pairs
    ]
    for text in texts:
        assert not any(unit in text for unit in units), text


# --- the transcribed ICH tables --------------------------------------------------------


def test_every_class_2_concentration_limit_agrees_with_its_pde() -> None:
    """The guideline's own ppm = PDE x 100 identity at a 10 g daily dose, over the whole table.

    A transcription's characteristic failure is a mistyped digit, and this is the one internal
    consistency check the table supports — it catches a transposed PDE or ppm without needing the
    source document open.

    **Class 2 only.** The identity is *tautological* for Class 3: both of its numbers are the one
    general 50 mg/day statement, so agreeing proves nothing about a transcription. Twenty-five of
    the sixty-two rows are Class 3, so including them would inflate the check's apparent coverage by
    more than a third while adding no evidence.
    """
    class_2 = [s for s in Q3C.solvents if s.solvent_class == "2"]
    assert len(class_2) == 32, "the Class 2 block is the bulk of Q3C; this is checking the table"
    for solvent in class_2:
        assert solvent.pde_mg_per_day is not None, solvent.name
        assert solvent.concentration_limit_ppm == pytest.approx(solvent.pde_mg_per_day * 100.0), (
            solvent.name
        )


def test_class_1_is_quoted_as_a_concentration_and_class_3_as_the_general_statement() -> None:
    """Which numbers a class *has* is part of the transcription, and getting it wrong invents one.

    Q3C assigns Class 1 no PDE and Class 3 no solvent-specific PDE. A Class 3 row carrying its own
    figure would be a number the guideline does not contain, published under a real citation — the
    exact failure this table was transcribed to end.
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
    """The one column relation Q3D actually holds, so a transposed pair of columns fails here.

    Deliberately *not* a full ordering: cadmium and selenium both quote an inhalation PDE above
    their parenteral one, so `oral >= parenteral >= inhalation` would assert a fiction and would be
    "fixed" by editing the numbers.
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
    """A collision hands a reader another substance's limit with a real ICH citation attached.

    `_register` refuses one while building the index; this reads the refusal off the public surface
    rather than trusting it, and it covers the two files *together* — a solvent and an element
    claiming one spelling is the case neither file can see alone.
    """
    for query, limit in ich.index().items():
        assert ich.impurity_limit(query).limit is limit


@pytest.mark.parametrize(
    "absent",
    ["tert-butyl alcohol", "tert-butanol", "water", "nickel", "Ni", "silver", "Ag", "gold", "Au"],
)
def test_a_deliberate_omission_is_still_omitted(absent: str) -> None:
    """The omissions are the honest half of the transcription and must not be "completed".

    Every one of these is a real substance a real guideline covers, left out because its value could
    not be verified against the source document (or, for water, because Q3C does not cover it). A
    contributor filling them in from memory would be reintroducing precisely the fabrication these
    tables replaced — and the lookup's own miss verdict already says the right thing: this system
    does not carry the number, read it from the guideline.
    """
    assert ich.impurity_limit(absent).limit is None


@pytest.mark.parametrize("ambiguous", ["EDC", "DMA", "TCE"])
def test_an_ambiguous_abbreviation_names_nothing_here(ambiguous: str) -> None:
    """Three abbreviations that name more than one substance must not resolve to either.

    `EDC` is ethylene dichloride in Q3C and the carbodiimide coupling reagent at the bench — and the
    second is in the vendored reagent table, so a chemist asking about their coupling reagent would
    otherwise be handed a Class 1 limit of 5 ppm with a genuine ICH citation on it. A wrong row is
    worse than the miss it replaces precisely because the citation makes it look checkable.
    """
    assert ich.impurity_limit(ambiguous).limit is None


# --- the vendored reagent table --------------------------------------------------------


def test_the_reagent_corpus_is_byte_identical_to_the_one_chem_ships() -> None:
    """This server carries a second copy of `chem`'s reagent table. It must be the *same* copy.

    One server never imports another, so a table two servers both need is carried by both — and two
    copies of one corpus is exactly how a chemist ends up with two answers about one substance. The
    duplication is made safe by being provable: identical bytes, therefore an identical checksum,
    therefore an identical manifest. `tests/test_fleet.py` asserts the same thing from outside, so
    the property survives either file being edited.

    If the two ever have to diverge, that is a decision with an argument behind it, and this test is
    where the argument gets written down rather than discovered.
    """
    here = reagents.DATA_DIR
    there = here.parents[4] / "chem" / "src" / "chemclaw_mcp_chem" / "data"
    assert (here / "records.csv").read_bytes() == (there / "records.csv").read_bytes()
    assert json.loads((here / "dataset.json").read_text(encoding="utf-8")) == json.loads(
        (there / "dataset.json").read_text(encoding="utf-8")
    )


def test_the_reagent_table_is_what_makes_a_structure_reach_an_ich_row() -> None:
    """The one thing `ich.py` asks of that table, pinned so a narrowing of it fails here.

    A SMILES is not a spelling of a name, so no amount of synonyms in the guideline files covers it.
    Drop the reagent table and `ich_impurity_limit("C1CCOC1")` becomes a miss — for a solvent whose
    row is right there — while the tool docstring goes on promising that it resolves.
    """
    assert reagents.resolve_compound_name("C1CCOC1") is not None
    assert ich.impurity_limit("C1CCOC1").limit is not None
