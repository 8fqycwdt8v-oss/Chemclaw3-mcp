"""What the three tools answer, and what they refuse. All offline, all against the real tables.

Three things must hold for an advisory safety screen to be worth having: the rules fire on real
examples of the motifs they name, they stay quiet on ordinary chemistry (a screen that cries wolf is
switched off), and nothing anywhere renders "no match" as "safe". The rule table is data, so these
tests pin its behavior with named molecules rather than mocking the matcher.

The last two sections cover this server's other two cited tables, which answer *different* questions
and must keep answering them separately: the genotoxicity structural alerts (`engine/genotox.py`)
and the transcribed ICH Q3C/Q3D limits (`engine/ich.py`). Both are here rather than in files of
their own because the property that matters most about them is how they relate to the hazard screen
— that a genotoxicity alert is not a process-safety flag, and that neither is a classification — and
that is only assertable with all three in one place.

Ported from Chemclaw3's `tests/test_safety.py`, minus the sections covering that repository's
`kg-validate` hazard gate, which reads agent-authored knowledge-graph notes. No such gate exists
here — a server answers questions and does not gate a pull request — so `science/safety/notes.py`
and its ~370 lines of tests came across as nothing at all rather than as something unreachable.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from chemclaw_mcp_safety.engine import screen as screen_module
from chemclaw_mcp_safety.engine.genotox import (
    ALERTS_DIR,
    ALERTS_FILE,
    AlertTable,
    screen_genotoxic_alerts,
)
from chemclaw_mcp_safety.engine.ich import impurity_limit
from chemclaw_mcp_safety.engine.screen import (
    MAX_COMPONENTS,
    SafetyRulesError,
    read_table,
    screen_reaction,
    screen_structure,
)
from chemclaw_mcp_safety.tools import ich_impurity_limit, screen_hazards

# One textbook example per structural rule.
_HAZARDOUS = {
    "organic-azide": "CCCN=[N+]=[N-]",  # 1-azidopropane
    "non-carbon-azide": "[Na+].[N-]=[N+]=[N-]",  # sodium azide
    "acyl-azide": "CC(=O)N=[N+]=[N-]",  # acetyl azide
    "diazo": "CC(=[N+]=[N-])C(=O)OC",  # methyl diazoacetate
    "diazonium": "c1ccccc1[N+]#N",  # benzenediazonium
    "peroxide": "CC(C)(C)OOC(C)(C)C",  # di-tert-butyl peroxide
    "nitrate-ester": "CCO[N+](=O)[O-]",  # ethyl nitrate
    "polynitro-aromatic": "O=[N+]([O-])c1ccccc1[N+](=O)[O-]",  # 1,2-dinitrobenzene
    "perchlorate": "OCl(=O)(=O)=O",  # perchloric acid
    "hydrazine": "NN",
    "n-halamine": "ClN1C(=O)CCC1=O",  # N-chlorosuccinimide
}

# The polynitroarenes, one per substitution pattern. Deliberately more than the one reference
# molecule `_HAZARDOUS` holds — see `test_polynitroarenes_flag_at_every_substitution_pattern`.
_POLYNITRO = {
    "1,2-dinitrobenzene": "O=[N+]([O-])c1ccccc1[N+](=O)[O-]",
    "1,3-dinitrobenzene": "O=[N+]([O-])c1cccc([N+](=O)[O-])c1",
    "1,4-dinitrobenzene": "O=[N+]([O-])c1ccc([N+](=O)[O-])cc1",
    "TNT": "Cc1c(cc(cc1[N+](=O)[O-])[N+](=O)[O-])[N+](=O)[O-]",
    "picric acid": "Oc1c(cc(cc1[N+](=O)[O-])[N+](=O)[O-])[N+](=O)[O-]",
}

# Everyday process chemistry that must raise nothing: the false-positive side of the screen.
_BENIGN = [
    "CCO",  # ethanol
    "CC(=O)O",  # acetic acid
    "CCOC(C)=O",  # ethyl acetate
    "c1ccccc1",  # benzene
    "CC(=O)Oc1ccccc1C(=O)O",  # aspirin
    "O=[N+]([O-])c1ccccc1",  # nitrobenzene — one nitro group is not the polynitro motif
    "CC(=O)NN",  # acetohydrazide — an acylated N-N, not free hydrazine
    "CC#N",  # acetonitrile
    "ClCCl",  # dichloromethane
    "OC(=O)c1ccccc1",  # benzoic acid
]


def _rule_corpus(directory: Path, body: str) -> Path:
    """Write a stand-in rule table plus the `dataset.json` `load_dataset` will verify it against.

    The checksum is computed here rather than pasted, because this file's subject is the *loader's*
    behaviour on a broken table, not the checksum arithmetic — a hand-typed digest would make every
    one of these tests fail for the wrong reason. The real corpus's checksum is pinned in
    `dataset.json` and asserted by `test_dataset.py`, which is where that property belongs.
    """
    directory.mkdir(parents=True, exist_ok=True)
    table = directory / screen_module.RULES_FILE
    table.write_text(body, encoding="utf-8")
    (directory / "dataset.json").write_text(
        json.dumps(
            {
                "name": "test-rules",
                "version": "0",
                "licence": "n/a",
                "retrieved_from": "written by this test",
                "description": "a stand-in rule table",
                "sha256": hashlib.sha256(table.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    return directory


# --- the hazard rule table -------------------------------------------------------------


@pytest.mark.parametrize(("rule_id", "smiles"), sorted(_HAZARDOUS.items()))
def test_each_rule_fires_on_its_reference_molecule(rule_id: str, smiles: str) -> None:
    """Every committed rule matches a textbook example of the motif it claims to detect.

    A SMARTS that stops matching fails *silently* — the screen just reports nothing, which reads as
    "no hazard" — so each rule is pinned to a molecule by name.
    """
    result = screen_structure(smiles)
    assert rule_id in {flag.rule_id for flag in result.flags}


@pytest.mark.parametrize(("name", "smiles"), sorted(_POLYNITRO.items()))
def test_polynitroarenes_flag_at_every_substitution_pattern(name: str, smiles: str) -> None:
    """TNT and picric acid must flag, not only the ortho isomer the old pattern happened to match.

    `polynitro-aromatic` shipped as a written six-atom ring chain (`[nitro]c1ccccc1[nitro]`), which
    hangs the second nitro group off the ring-closure atom and therefore matches **ortho only**.
    TNT, picric acid and both the meta and para dinitrobenzenes screened clean, and no other rule
    caught them: the screen answered "no rule in the hazard table matched" about high explosives.

    **The interesting part is why a green test suite allowed it.** The discipline was one reference
    molecule per rule, and every fixture picked 1,2-dinitrobenzene — the single arrangement the
    broken pattern *did* match. One example is a complete test of a rule that names a motif, and a
    blind one for a rule whose own words say "multiple" or "on one ring": those semantics are a
    count and a set of relative positions, so the discipline has to be one molecule per arrangement
    claimed.
    """
    assert "polynitro-aromatic" in {flag.rule_id for flag in screen_structure(smiles).flags}, name


def test_a_mononitroarene_is_not_polynitro() -> None:
    """A count of two: one nitro group on a ring must not fire the polynitro rule.

    Pinned separately from the benign list because it is what makes the count real: `min_matches`
    wired up as `>= 1` would satisfy every match assertion above while turning the archetypal
    explosive alert into "contains a nitro group", and a flag that fires on nitrobenzene is a flag
    people learn to scroll past.
    """
    flags = {flag.rule_id for flag in screen_structure("O=[N+]([O-])c1ccccc1").flags}
    assert "polynitro-aromatic" not in flags


@pytest.mark.parametrize("smiles", _BENIGN)
def test_ordinary_chemistry_raises_no_flag(smiles: str) -> None:
    """Common solvents, reagents and products stay quiet — a screen that cries wolf is ignored."""
    assert screen_structure(smiles).flags == []


def test_a_flag_carries_its_explanation_and_citation() -> None:
    """A flag must be actionable and traceable: severity, why it matters, and a source."""
    flag = screen_structure(_HAZARDOUS["organic-azide"]).flags[0]
    assert flag.severity == "high"
    assert "azide" in flag.explanation.lower()
    assert flag.citation
    assert flag.matched == _HAZARDOUS["organic-azide"]


@pytest.mark.parametrize(
    ("smiles", "reagent"),
    [
        ("[N-]=[N+]=[N-]", "bare azide anion"),
        ("[Na+].[N-]=[N+]=[N-]", "sodium azide"),
        ("[K+].[N-]=[N+]=[N-]", "potassium azide"),
        ("[NH4+].[N-]=[N+]=[N-]", "ammonium azide"),
        ("N=[N+]=[N-]", "hydrazoic acid"),
        ("C[Si](C)(C)N=[N+]=[N-]", "trimethylsilyl azide"),
        ("O=P(OC1=CC=CC=C1)(OC1=CC=CC=C1)N=[N+]=[N-]", "diphenylphosphoryl azide"),
    ],
)
def test_azide_not_bonded_to_carbon_is_flagged(smiles: str, reagent: str) -> None:
    """Every azide that is not carbon-bound flags, not just the organic ones.

    Sodium azide is one of the most-reached-for reagents in the building, and it screened *clean*:
    `organic-azide` and `acyl-azide` both open on `[#6]`, so a salt matched neither and the screen
    reported nothing — which a reader takes as "no hazard found" on a compound that is acutely toxic
    and liberates explosive HN3 on contact with acid. The same hole swallowed hydrazoic acid and the
    silyl/phosphoryl azide transfer reagents, so each is pinned here by name.
    """
    flags = {flag.rule_id for flag in screen_structure(smiles).flags}
    assert "non-carbon-azide" in flags, f"{reagent} screened clean"


@pytest.mark.parametrize("smiles", ["CCCN=[N+]=[N-]", "CC(=O)N=[N+]=[N-]"])
def test_carbon_bound_azides_do_not_also_fire_the_non_carbon_rule(smiles: str) -> None:
    """The new rule stays off carbon-bound azides — two flags for one motif is noise, not safety."""
    flags = {flag.rule_id for flag in screen_structure(smiles).flags}
    assert "non-carbon-azide" not in flags
    assert flags & {"organic-azide", "acyl-azide"}  # still caught by the rule that owns them


def test_an_empty_result_never_says_safe() -> None:
    """The no-match verdict states what was actually checked, never that the chemistry is safe.

    An over-trusted screen is more dangerous than no screen: it converts an absence of knowledge
    into apparent assurance.
    """
    verdict = screen_structure("CCO").verdict.lower()
    assert "no rule" in verdict  # says what was actually checked
    assert "not a safety assessment" in verdict  # and what it is not
    # No phrasing a reader could take as a clearance.
    assert not any(claim in verdict for claim in ("is safe", "no hazard", "safe to"))


def test_incompatible_pair_is_only_visible_at_reaction_level() -> None:
    """An oxidizer and a reducing agent are unremarkable alone and flagged together.

    This is the whole reason `screen_reaction` exists: no per-molecule screen can see it.
    """
    permanganate = "[K+].[O-][Mn](=O)(=O)=O"
    hydride = "[Li+].[AlH4-]"
    assert screen_structure(permanganate).flags == []
    assert screen_structure(hydride).flags == []
    pair = screen_reaction([permanganate, hydride, "CCO"])
    assert [flag.rule_id for flag in pair.flags] == ["oxidizer-with-reductant"]
    assert "+" in pair.flags[0].matched  # names both species, so the chemist sees the combination


def test_flags_are_ordered_worst_first() -> None:
    """The most serious flag leads, so a reader who stops after one line reads the right one."""
    result = screen_reaction(["NN", _HAZARDOUS["organic-azide"]])  # medium + high
    assert [flag.severity for flag in result.flags] == ["high", "medium"]
    assert result.max_severity == "high"


def test_unparseable_smiles_is_a_clear_error() -> None:
    """A bad structure is an error, not an empty (reassuring) result."""
    with pytest.raises(SafetyRulesError, match="invalid SMILES"):
        screen_structure("not-a-molecule(((")


def test_a_structure_with_trailing_text_is_refused_and_not_quietly_narrowed() -> None:
    """The test that would have caught it: trailing garbage must not screen as a clean result.

    RDKit's SMILES parser accepts a valid *prefix* and ignores whatever follows a space, so
    `screen_structure` used to answer this call with zero flags, the verdict "No rule in the hazard
    table matched", and `screened == ["CCO"]` — a clean screen of **ethanol**, for an input whose
    ignored tail is an azide. That is the worst failure mode available to this module: its own
    docstring says an empty result must never read as a clearance, and here the empty result was not
    even about the molecule asked about.

    Asserted as a refusal *and* as an absence of a clean result, because the second is the part that
    was wrong: a test that only pinned the exception would pass against a version that returned
    `ScreenResult(flags=[], screened=["CCO"])`.
    """
    concatenated = f"CCO {_HAZARDOUS['organic-azide']}"
    assert screen_structure(_HAZARDOUS["organic-azide"]).flags  # the tail alone is a real flag

    with pytest.raises(SafetyRulesError, match="invalid SMILES"):
        screen_structure(concatenated)


def test_an_empty_string_is_refused_rather_than_screened_as_a_molecule() -> None:
    """RDKit parses `""` to a molecule with no atoms, which matches no rule and reads as clean."""
    with pytest.raises(SafetyRulesError, match="invalid SMILES"):
        screen_structure("")


def test_a_reaction_refusal_names_which_component_it_could_not_read() -> None:
    """A chemist told "one of these nine is unusable" cannot act on it; the position is the fix.

    The refusal counts positions in the list *as given*, so it points at the string the caller wrote
    rather than at an index into the deduplicated set the screen works on.
    """
    with pytest.raises(SafetyRulesError, match="component 2 of 3"):
        screen_reaction(["CCO", f"CCO {_HAZARDOUS['organic-azide']}", "O"])


def test_a_screened_reaction_still_echoes_what_it_looked_at() -> None:
    """The refusal above does not cost a good call its `screened` list.

    `screened` is the evidence that a screen is about the molecules the caller meant: it is the
    canonical form of every structure parsed, deduplicated, so two spellings of one substance appear
    once.
    """
    result = screen_reaction(["OCC", "CCO", "O"])
    assert result.screened == ["CCO", "O"]


def test_a_screen_of_nothing_is_refused_rather_than_answered_cleanly() -> None:
    """An empty list used to produce a clean screen of nothing, which is the whole thesis inverted.

    `screen_hazards([])` returned `flags=[]`, `screened=[]` and "No rule in the hazard table
    matched. This is not a safety assessment." — the exact payload a clean screen produces, with
    nothing in it to say that no molecule was ever looked at. `screened` exists so a clean result
    names its subject; an empty result with an empty `screened` is the one case where that evidence
    is missing and the verdict still reads like an answer.
    """
    for screen in (screen_reaction, screen_genotoxic_alerts):
        with pytest.raises(SafetyRulesError, match="at least one structure"):
            screen([])


def test_a_missing_rule_table_fails_loudly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing table stops the screen instead of silently reporting no hazards.

    Screening with half a rule table would report "no rule matched" for a hazard the table covers —
    the exact failure this module exists to prevent, so it is fatal, not skipped. The filename is in
    the message: `read_table` is shared with the genotoxicity screen and both ICH tables, and a
    reader sent to the wrong file by a generic "hazard rules" is the confusion this package spends
    the most effort preventing.
    """
    monkeypatch.setattr(screen_module, "RULES_DIR", tmp_path / "missing")
    with pytest.raises(SafetyRulesError, match=r"cannot read the safety table rules\.yaml"):
        screen_structure("CCO")


def test_a_rule_table_that_is_not_the_approved_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corpus edited after review is refused by its checksum, not screened against.

    This is what the vendored-dataset contract buys a safety table specifically: a rule table
    truncated by a bad COPY or swapped in a rebuild is a *shorter* rule table, and a shorter rule
    table answers "no rule matched" — which is indistinguishable from a clean molecule and is the
    one outcome this whole server is built around not producing.
    """
    directory = _rule_corpus(tmp_path / "tampered", "structural: []\nincompatible_pairs: []\n")
    (directory / screen_module.RULES_FILE).write_text("structural: []\n", encoding="utf-8")
    monkeypatch.setattr(screen_module, "RULES_DIR", directory)
    with pytest.raises(SafetyRulesError, match="does not match the approved checksum"):
        screen_structure("CCO")


def test_a_malformed_rule_table_names_the_broken_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unparseable SMARTS names the rule that owns it, so the table is fixable."""
    directory = _rule_corpus(
        tmp_path / "broken",
        "structural:\n"
        "  - id: broken-rule\n"
        '    smarts: "[not-a-smarts"\n'
        "    severity: high\n"
        "    explanation: x\n"
        "    citation: y\n",
    )
    monkeypatch.setattr(screen_module, "RULES_DIR", directory)
    with pytest.raises(SafetyRulesError, match="broken-rule"):
        screen_structure("CCO")


def test_an_empty_rule_table_is_refused_rather_than_screened_against(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A table with no rules in it answers "nothing matched" about everything, forever."""
    directory = _rule_corpus(tmp_path / "empty", "structural: []\nincompatible_pairs: []\n")
    monkeypatch.setattr(screen_module, "RULES_DIR", directory)
    with pytest.raises(SafetyRulesError, match="contain no rules"):
        screen_structure("CCO")


# --- the agent-facing tools ------------------------------------------------------------


def test_tool_screens_one_molecule_and_a_reaction() -> None:
    """The tool screens a single structure alone and a component list as a reaction."""
    single = asyncio.run(screen_hazards([_HAZARDOUS["peroxide"]]))
    assert [flag.rule_id for flag in single.flags] == ["peroxide"]
    reaction = asyncio.run(screen_hazards(["[K+].[O-][Mn](=O)(=O)=O", "[Li+].[AlH4-]"]))
    assert [flag.rule_id for flag in reaction.flags] == ["oxidizer-with-reductant"]


def test_the_limit_tool_answers_the_same_as_its_engine() -> None:
    """`ich_impurity_limit` is a pass-through and must stay one — no reshaping on the way out."""
    assert asyncio.run(ich_impurity_limit("Pd")).model_dump() == impurity_limit("Pd").model_dump()


def _distinct_pair_matching(n: int, left: str, right: str) -> list[str]:
    """`n` distinct SMILES of constant size, half matching each side of a pair rule.

    Constant size matters and is the reason for the atom-map labels: generating variety by growing a
    chain makes the *total atom count* quadratic too, so a timing curve measures the input rather
    than the code.
    """
    half = n // 2
    return [left.format(i=i + 1) for i in range(half)] + [
        right.format(i=i + 1) for i in range(n - half)
    ]


def test_a_reaction_screen_refuses_more_components_than_it_can_screen() -> None:
    """Pair rules are a cross-product, so an oversized list is refused before any matching.

    The measured defect: 13 KiB of SMILES produced 251,000 flags and blocked the serving connector's
    event loop for 2.48 s. A request-size cap is no bound on it, because the amplification is in the
    *response*. Raise `CHEMCLAW_SAFETY_MAX_COMPONENTS` and this fails.
    """
    oversized = _distinct_pair_matching(MAX_COMPONENTS + 1, "[NH2:{i}]N", "[OH:{i}]O")
    with pytest.raises(SafetyRulesError, match="at most"):
        screen_reaction(oversized)


def test_a_genotoxicity_screen_refuses_the_same_way() -> None:
    """The sibling screen has the identical cross-product shape and had the identical hole.

    Only `screen_hazards` was reported; `screen_genotoxic_alerts` was measured at 640 components
    producing 102,400 alerts in 933 ms. One bound, both callers — a screen that refused in one place
    and not the other would just move the defect.
    """
    oversized = _distinct_pair_matching(MAX_COMPONENTS + 1, "C[NH:{i}]C", "[O:{i}]=NO")
    with pytest.raises(SafetyRulesError, match="at most"):
        screen_genotoxic_alerts(oversized)


def test_the_bound_admits_a_real_reaction_unchanged() -> None:
    """A limit that refused real chemistry would be worse than the defect it closes.

    The largest shipped ELN entry has well under a dozen species; this pins that a component list at
    the limit still screens, and still finds the pair flag it should.
    """
    at_limit = ["[K+].[O-][Mn](=O)(=O)=O", "[Li+].[AlH4-]"] + [
        f"[CH4:{i + 1}]" for i in range(MAX_COMPONENTS - 2)
    ]
    assert len(at_limit) == MAX_COMPONENTS
    assert [flag.rule_id for flag in screen_reaction(at_limit).flags] == ["oxidizer-with-reductant"]


def test_a_clean_screen_carries_its_disclaimer_into_the_serialized_result() -> None:
    """The "not a safety assessment" line must survive `model_dump()`, not just exist.

    A bare `property` is dropped by pydantic serialization, so a clean screen reached the model as
    `{"flags": []}` and the caveat never entered the context window the answer was written from.
    Asserting on the dumped payload — not on the attribute — is the whole point: reading
    `result.verdict` in a test passes either way.
    """
    dumped = screen_structure("CCO").model_dump()
    assert "verdict" in dumped, "verdict is not serialized; a clean screen reads as an empty result"
    assert "not a safety assessment" in dumped["verdict"]
    assert "safe" not in dumped["verdict"].lower().replace("safety", "")


def test_a_flagged_screen_serializes_an_advisory_verdict_too() -> None:
    """The matched case must say advisory-only in the payload, for the same reason."""
    dumped = screen_structure("CC(=O)OOC(C)=O").model_dump()
    assert dumped["flags"], "diacetyl peroxide must raise the peroxide rule"
    assert "Advisory only" in dumped["verdict"]


# --- the four blind spots a live run confirmed -----------------------------------------

# Each molecule screened clean before its rule was widened, and each is an ordinary bench reagent
# for the hazard class its rule is named after. Kept as (name, SMILES, rule) so a future narrowing
# of any pattern names the compound it would silence.
_PREVIOUSLY_SILENT = [
    ("sodium peroxide", "[O-][O-].[Na+].[Na+]", "peroxide"),
    ("1,1-dimethylhydrazine (UDMH)", "CN(C)N", "hydrazine"),
    ("chloramine-T", "CC1=CC=C(C=C1)S(=O)(=O)[N-]Cl.[Na+]", "n-halamine"),
]


@pytest.mark.parametrize(("name", "smiles", "rule"), _PREVIOUSLY_SILENT)
def test_a_previously_silent_hazard_now_fires(name: str, smiles: str, rule: str) -> None:
    """A textbook member of a covered hazard class must not screen clean.

    Sodium peroxide writes its oxygens as one-coordinate anions, UDMH carries H on only one
    nitrogen, and chloramine-T's nitrogen is anionic and two-coordinate — each fell outside a
    pattern written for the neutral, fully-substituted case. A silent rule is the one failure mode
    this module exists to prevent, because the screen reports it as "nothing matched".
    """
    assert rule in {flag.rule_id for flag in screen_structure(smiles).flags}, name


def test_a_peroxide_salt_is_an_oxidizer_to_the_pair_rule_as_well() -> None:
    """The widening that made `peroxide` see Na2O2 must reach the pair rule it also belongs to.

    Measured before the fix: `H2O2 + NaBH4` raised ['oxidizer-with-reductant', 'peroxide'] while
    `Na2O2 + NaBH4` raised ['peroxide'] alone. A strong solid oxidiser mixed with a complex hydride
    is the case the rule is named for, and it was the one that did not fire.
    """
    hydride = "[BH4-].[Na+]"
    for oxidizer in ("OO", "[O-][O-].[Na+].[Na+]"):
        fired = {f.rule_id for f in screen_reaction([oxidizer, hydride]).flags}
        assert "oxidizer-with-reductant" in fired, oxidizer
    # The widening must not turn every anionic oxygen into an oxidizer: a carboxylate salt and a
    # nitro group both carry `[OX1-]` without a peroxide bond.
    for innocent in ("CC(=O)[O-].[Na+]", "O=[N+]([O-])c1ccccc1"):
        assert "oxidizer-with-reductant" not in {
            f.rule_id for f in screen_reaction([innocent, hydride]).flags
        }, innocent


def test_a_hydrazinium_salt_is_a_hydrazine_to_both_rules() -> None:
    """The salt is how hydrazine is actually weighed out, and it screened clean on both rules.

    Protonating a hydrazine makes that nitrogen `NX4+`, so `[NX3;H2,H1]` stopped matching: hydrazine
    monohydrochloride and hydrazine sulfate raised neither the structural `hydrazine` rule nor
    `oxidizer-with-reductant` beside an oxidiser, while free hydrazine raised both. Same class, same
    hazard, same waste stream — and the protonated spelling is the ordinary catalogue form.
    """
    salts = ("[NH3+]N.[Cl-]", "[NH3+]N.[O-]S([O-])(=O)=O", "[NH3+][NH3+].[Cl-].[Cl-]")
    for salt in salts:
        assert "hydrazine" in {f.rule_id for f in screen_structure(salt).flags}, salt
        assert "oxidizer-with-reductant" in {
            f.rule_id for f in screen_reaction([salt, "OO"]).flags
        }, salt
    for innocent in (
        "[NH4+].[Cl-]",  # ammonium chloride
        "[NH3+]CC[NH3+].[Cl-].[Cl-]",  # ethylenediamine dihydrochloride
        "[NH3+]O.[Cl-]",  # hydroxylamine hydrochloride
        "[NH3+]c1ccccc1.[Cl-]",  # aniline hydrochloride
        "NC(=O)N[NH3+].[Cl-]",  # semicarbazide hydrochloride — acylated, so a hydrazide
    ):
        assert "hydrazine" not in {f.rule_id for f in screen_structure(innocent).flags}, innocent


# A 1,1-disubstituted hydrazine beside an oxidiser, kept as (name, SMILES, rule) for the same reason
# `_PREVIOUSLY_SILENT` is. UDMH + H2O2 / N2O4 is the archetypal hypergolic pair — it ignites on
# contact, with no ignition source — and it is the exact molecule the structural rule already
# learned about once.
_HYPERGOLIC = [
    ("UDMH (1,1-dimethylhydrazine)", "CN(C)N", "oxidizer-with-reductant"),
    ("1,1-dimethylhydrazinium chloride", "C[NH+](C)N.[Cl-]", "oxidizer-with-reductant"),
    ("N-aminopiperidine", "NN1CCCCC1", "oxidizer-with-reductant"),
    ("N-aminomorpholine", "NN1CCOCC1", "oxidizer-with-reductant"),
]


@pytest.mark.parametrize(("name", "smiles", "rule"), _HYPERGOLIC)
def test_a_disubstituted_hydrazine_is_a_reductant_to_the_pair_rule(
    name: str, smiles: str, rule: str
) -> None:
    """The pair rule kept the H-on-both-nitrogens form its structural twin had already dropped.

    Measured before the fix: `NN + OO` and `[NH3+]N.[Cl-] + OO` both raised
    `oxidizer-with-reductant`, while `CN(C)N + OO` raised only `hydrazine` and `peroxide` — the
    hypergolic pair was the one that screened clean on the rule named for it.
    """
    assert rule in {f.rule_id for f in screen_reaction([smiles, "OO"]).flags}, name


# 1,2-diarylhydrazines: routine nitrobenzene-reduction products and benzidine-rearrangement
# precursors, silenced by an `!$(N[a])` guard whose stated purpose (keeping azo systems out) is
# served entirely by `NX3` — an azo nitrogen is `NX2` and never matched either way.
_ARYL_HYDRAZINES = [
    ("1,2-diphenylhydrazine (hydrazobenzene)", "c1ccccc1NNc1ccccc1", "hydrazine"),
    ("1-methyl-1-phenylhydrazine", "CN(N)c1ccccc1", "hydrazine"),
]


@pytest.mark.parametrize(("name", "smiles", "rule"), _ARYL_HYDRAZINES)
def test_a_diarylhydrazine_is_still_a_hydrazine(name: str, smiles: str, rule: str) -> None:
    """A guard aimed at azo systems hit hydrazines instead, and only when *both* N were aryl.

    Measured with and without `!$(N[a])`: azobenzene was False either way, phenylhydrazine True
    either way, and hydrazobenzene was the single molecule the guard changed — from True to False.
    It bit only when both nitrogens are aryl-bound, because with one aryl nitrogen the match is
    simply found from the other direction, which is why it looked harmless.
    """
    assert rule in {f.rule_id for f in screen_structure(smiles).flags}, name


def test_an_azo_compound_is_excluded_by_coordination_not_by_an_aryl_guard() -> None:
    """The reason azo systems stay out, pinned so the guard is not reintroduced to "restore" it.

    An azo nitrogen is two-coordinate with no hydrogen; `[NX3,NX4+]` cannot match it. This holds for
    the aryl and the alkyl case alike, which an `!$(N[a])` guard never covered.
    """
    for name, smiles in (
        ("azobenzene", "c1ccc(cc1)/N=N/c1ccccc1"),
        ("azoxybenzene", "c1ccccc1[N+]([O-])=Nc1ccccc1"),
        ("diethyl azodicarboxylate (DEAD)", "CCOC(=O)/N=N/C(=O)OCC"),
        ("azo-tert-butane", "CC(C)(C)/N=N/C(C)(C)C"),
    ):
        assert "hydrazine" not in {f.rule_id for f in screen_structure(smiles).flags}, name


def test_a_complex_hydride_fires_against_a_vicinal_dichloride_too() -> None:
    """1,2-dichloroethane carries the same incompatibility as DCM and was silent.

    The pair rule matched geminal dichlorides only, so an ordinary process solvent paired with
    LiAlH4 raised nothing.
    """
    flags = screen_reaction(["[Li+].[AlH4-]", "ClCCCl"]).flags
    assert "complex-hydride-with-chlorinated-solvent" in {f.rule_id for f in flags}


@pytest.mark.parametrize(
    ("name", "smiles"),
    [
        # Widening a hazard rule until it fires on everything is worse than the gap it closed: a
        # rule that flags a routine reagent teaches a chemist to skip reading the flags.
        ("1-chlorobutane", "CCCCCl"),
        ("benzyl chloride", "ClCc1ccccc1"),
        ("acetyl chloride", "CC(=O)Cl"),
        ("epichlorohydrin", "ClCC1CO1"),
        ("2-chloroethanol", "OCCCl"),
        ("aniline", "Nc1ccccc1"),
        ("acetohydrazide", "CC(=O)NN"),
        ("azobenzene", "c1ccc(cc1)/N=N/c1ccccc1"),
        ("ethylene glycol", "OCCO"),
        ("1,4-dioxane", "C1COCCO1"),
        ("ethyl acetate", "CCOC(C)=O"),
        ("4-chloroanisole", "COc1ccc(Cl)cc1"),
    ],
)
def test_widening_a_rule_did_not_make_a_routine_reagent_hazardous(name: str, smiles: str) -> None:
    """None of the widened patterns may fire on an everyday, unremarkable reagent."""
    widened = {"peroxide", "hydrazine", "n-halamine"}
    assert widened.isdisjoint({f.rule_id for f in screen_structure(smiles).flags}), name


# The false-positive half of dropping the H requirement and the aryl guard. Every one of these
# carries a nitrogen the widened pattern could plausibly reach — a second nitrogen, a cation, an
# N-O bond, an aryl amine, an acylated N-N - and none of them is a hydrazine. A widened safety
# pattern that cries wolf gets the whole screen switched off, so this half is load-bearing.
_NOT_A_HYDRAZINE = [
    ("ammonium chloride", "[NH4+].[Cl-]"),
    ("ethylenediamine", "NCCN"),
    ("ethylenediamine dihydrochloride", "[NH3+]CC[NH3+].[Cl-].[Cl-]"),
    ("piperazine", "C1CNCCN1"),
    ("DABCO", "C1CN2CCN1CC2"),
    ("triethylamine", "CCN(CC)CC"),
    ("aniline", "Nc1ccccc1"),
    ("aniline hydrochloride", "[NH3+]c1ccccc1.[Cl-]"),
    ("N,N-dimethylaniline", "CN(C)c1ccccc1"),
    ("hydroxylamine", "NO"),
    ("hydroxylamine hydrochloride", "[NH3+]O.[Cl-]"),
    ("urea", "NC(=O)N"),
    ("guanidine", "NC(N)=N"),
    ("imidazole", "c1cnc[nH]1"),
    ("pyrazole", "c1cc[nH]n1"),  # aromatic N-N, not a free hydrazine
    ("morpholine", "C1COCCN1"),
    ("acetohydrazide", "CC(=O)NN"),  # acylated, so a hydrazide
    ("tert-butyl carbazate (Boc-hydrazine)", "CC(C)(C)OC(=O)NN"),
    ("semicarbazide hydrochloride", "NC(=O)N[NH3+].[Cl-]"),
    # Not listed: tosylhydrazide. `!$(NC=O)` excludes *acyl* hydrazides, not sulfonyl ones, so it
    # fires and correctly so: TsNHNH2 is a free N-H hydrazine and a diimide-forming reductant,
    # is what the rule's prose names.
    ("tetramethylhydrazine", "CN(C)N(C)C"),  # a hydrazine with no N-H: out of scope
    ("acetone hydrazone", "CC(C)=NN"),  # the sp2 nitrogen is `NX2`
    ("DMF", "CN(C)C=O"),
    ("nitrobenzene", "O=[N+]([O-])c1ccccc1"),
]


@pytest.mark.parametrize(("name", "smiles"), _NOT_A_HYDRAZINE)
def test_the_widened_hydrazine_pattern_stays_quiet_on_ordinary_nitrogen(
    name: str, smiles: str
) -> None:
    """Neither hydrazine rule may fire on a molecule that merely contains nitrogen.

    Measured over a 106-row panel — the 61 distinct structures of the reagent identity table plus 45
    hand-picked hydrazine-adjacent and nitrogen-bearing reagents — exactly five molecules changed
    verdict across both rules, every one a hydrazine. Nothing in this list moved, in either
    direction.
    """
    fired = {f.rule_id for f in screen_reaction([smiles, "OO"]).flags}
    assert "hydrazine" not in fired, name
    assert "oxidizer-with-reductant" not in fired, name


# --- the genotoxicity alert table ------------------------------------------------------


# One published example per alert, plus the molecule each alert must *not* fire on. The negative
# half is what keeps a widened pattern from turning the list into noise: a table that flags every
# cross-coupling is a table a chemist stops reading.
_ALERTS = {
    "n-nitroso": ("CN(C)N=O", "CN(C)C=O"),  # NDMA vs DMF
    "aromatic-nitro": ("O=[N+]([O-])c1ccccc1", "C[N+](=O)[O-]"),  # nitrobenzene vs nitromethane
    "primary-aromatic-amine": ("Nc1ccccc1", "CC(=O)Nc1ccccc1"),  # aniline vs acetanilide
    "aromatic-azo": ("c1ccccc1N=Nc1ccccc1", "CCN=NCC"),  # azobenzene vs an aliphatic azo
    "epoxide": ("C1CO1", "C1CCOC1"),  # ethylene oxide vs THF
    "aziridine": ("C1CN1", "C1CCNC1"),  # aziridine vs pyrrolidine
    "alkyl-halide": ("CI", "CC(C)(C)Cl"),  # methyl iodide vs a tertiary chloride
    "alkyl-sulfonate-or-sulfate-ester": ("COS(C)(=O)=O", "CS(=O)(=O)O"),  # MeOMs vs MsOH
    "michael-acceptor": ("NC(=O)C=C", "CCC(N)=O"),  # acrylamide vs propionamide
    # Phenyl vinyl sulfone vs ethyl phenyl sulfone: the alkene is the alert, not the sulfone.
    "vinyl-sulfone": ("C=CS(=O)(=O)c1ccccc1", "CCS(=O)(=O)c1ccccc1"),
}


def test_every_structural_alert_has_a_worked_example_and_a_counterexample() -> None:
    """A row added to the table without a molecule beside it is a row nothing checks.

    The vinyl-sulfone gap below lived in the table because the motif was claimed in an
    explanation rather than encoded in a pattern, and no test named a molecule that had to
    match. Tying the parametrisation to the table itself is what makes the next row's absence
    fail here instead of in a screen.
    """
    table = read_table(ALERTS_DIR, ALERTS_FILE, AlertTable)
    assert {alert.id for alert in table.structural} == set(_ALERTS)


@pytest.mark.parametrize(("alert_id", "pair"), sorted(_ALERTS.items()))
def test_each_alert_fires_on_its_example_and_stays_quiet_on_its_counterexample(
    alert_id: str, pair: tuple[str, str]
) -> None:
    """Every alert matches a published example of its motif and not the near miss beside it."""
    hit, miss = pair
    assert alert_id in {a.alert_id for a in screen_genotoxic_alerts([hit]).alerts}
    assert alert_id not in {a.alert_id for a in screen_genotoxic_alerts([miss]).alerts}


@pytest.mark.parametrize(
    ("name", "smiles"),
    [
        ("phenyl vinyl sulfone", "C=CS(=O)(=O)c1ccccc1"),
        ("divinyl sulfone", "C=CS(=O)(=O)C=C"),
        ("ethyl vinyl sulfone", "C=CS(=O)(=O)CC"),
        ("vinyl sulfonamide", "C=CS(=O)(=O)N"),
    ],
)
def test_a_vinyl_sulfone_raises_an_alkylating_alert(name: str, smiles: str) -> None:
    """The false negative that mattered most: a claimed motif that no pattern could match.

    `michael-acceptor` requires a carbonyl carbon conjugated to the alkene, so a vinyl sulfone —
    an electrophilic warhead with no carbonyl anywhere — screened clean while the alert's own
    explanation told the reader vinyl sulfones matched. A screen that names a motif it cannot
    see is worse than one that stays silent about it, because the miss reads as a pass.
    """
    fired = {alert.alert_id for alert in screen_genotoxic_alerts([smiles]).alerts}
    assert "vinyl-sulfone" in fired, name


@pytest.mark.parametrize(
    ("name", "smiles"),
    [
        ("ethyl phenyl sulfone", "CCS(=O)(=O)c1ccccc1"),  # no alkene at all
        ("allyl methyl sulfone", "C=CCS(=O)(=O)C"),  # alkene present, not conjugated to the S
        ("methanesulfonic acid", "CS(=O)(=O)O"),
    ],
)
def test_an_unactivated_sulfone_stays_quiet(name: str, smiles: str) -> None:
    """Sulfones are ordinary chemistry; only the alkene *on* the sulfonyl is the alert."""
    fired = {alert.alert_id for alert in screen_genotoxic_alerts([smiles]).alerts}
    assert "vinyl-sulfone" not in fired, name


def test_a_nitrosating_agent_meeting_an_amine_flags_the_formation_route() -> None:
    """The nitrosamine question the run fabricated: an amine plus a nitrosating agent.

    Neither component is an alert on its own — DIPEA is an everyday base and sodium nitrite is an
    everyday reagent — so this is only visible across a component list, which is why it is a pair
    rule rather than a structural one.
    """
    together = screen_genotoxic_alerts(["CCN(C(C)C)C(C)C", "[Na+].[O-]N=O"])
    assert [a.alert_id for a in together.alerts] == ["nitrosatable-amine-with-nitrosating-agent"]
    assert screen_genotoxic_alerts(["CCN(C(C)C)C(C)C"]).alerts == []
    assert screen_genotoxic_alerts(["[Na+].[O-]N=O"]).alerts == []


def test_an_amide_is_not_treated_as_a_nitrosatable_amine() -> None:
    """DMF with sodium nitrite must stay quiet — an amide nitrogen is not the risk motif.

    The pair rule's value depends on it firing where nitrosation is plausible. Matching every
    nitrogen would fire on most reactions in the corpus and be ignored within a week.
    """
    assert screen_genotoxic_alerts(["CN(C)C=O", "[Na+].[O-]N=O"]).alerts == []


def test_every_alert_carries_a_citation_and_the_motif_it_names() -> None:
    """A flag a chemist cannot trace is a flag they must take on trust — which is the failure."""
    for alert in screen_genotoxic_alerts(["CN(C)N=O", "Nc1ccccc1"]).alerts:
        assert alert.citation.strip() and alert.motif.strip()
        assert alert.explanation.strip()


@pytest.mark.parametrize("smiles", [["CN(C)N=O"], ["CCO"]])
def test_the_result_says_a_flag_is_an_alert_and_not_a_classification(smiles: list[str]) -> None:
    """The disclaimer rides in the payload, on a hit *and* on a miss, not only in a docstring.

    `ScreenResult.verdict` was made a `computed_field` for exactly this reason: a plain property is
    not serialized, so the caveat never reached the model that had to write the answer. The four
    things this system cannot produce are named individually, because "expert assessment required"
    on its own did not stop the live run inventing an ICH M7 class and a worked purge factor.
    """
    rendered = screen_genotoxic_alerts(smiles).model_dump()
    verdict = rendered["verdict"]
    assert "ICH M7" in verdict and "purge factor" in verdict and "acceptable intake" in verdict
    assert "expert assessment" in verdict


def test_a_clean_alert_screen_is_not_reported_as_a_negative_prediction() -> None:
    """An empty result is ten patterns not matching, not a (Q)SAR calling the compound clean."""
    verdict = screen_genotoxic_alerts(["CCO"]).verdict
    assert "not a negative mutagenicity prediction" in verdict


def test_the_two_screens_stay_separate() -> None:
    """The genotoxicity table must not leak into the process-safety screen, or vice versa.

    This is the conflation the split exists to prevent, and it is testable in both directions.
    Nitrobenzene is the case that proves it: an ordinary reagent the hazard table is right to pass
    and the alert table is right to flag.
    """
    assert screen_structure("O=[N+]([O-])c1ccccc1").flags == []
    assert [a.alert_id for a in screen_genotoxic_alerts(["O=[N+]([O-])c1ccccc1"]).alerts] == [
        "aromatic-nitro"
    ]
    # And the other way: an organic azide is a process-safety flag with no genotoxicity alert.
    assert "organic-azide" in {f.rule_id for f in screen_structure("CCCN=[N+]=[N-]").flags}
    assert screen_genotoxic_alerts(["CCCN=[N+]=[N-]"]).alerts == []


def test_an_unparseable_component_stops_the_alert_screen() -> None:
    """A component that cannot be parsed must not silently screen as "no alerts"."""
    with pytest.raises(SafetyRulesError, match="invalid SMILES"):
        screen_genotoxic_alerts(["not-a-molecule"])


def test_a_component_with_trailing_text_stops_the_alert_screen_too() -> None:
    """The same silent narrowing, on the screen where a clean result is hedged hardest.

    `"CCO O=[N+]([O-])c1ccccc1"` used to parse as ethanol, drop the nitroarene after the space, and
    come back with no alerts — under a verdict spending three lines explaining that an empty list is
    not a negative mutagenicity prediction, about a molecule the payload never identifies. Refusing
    is the only answer that does not require the reader to know which of the two things the
    emptiness meant.
    """
    nitroarene = "O=[N+]([O-])c1ccccc1"
    assert screen_genotoxic_alerts([nitroarene]).alerts  # the ignored tail is a real alert

    with pytest.raises(SafetyRulesError, match="component 1 of 1"):
        screen_genotoxic_alerts([f"CCO {nitroarene}"])


# --- the ICH Q3C / Q3D reference tables ------------------------------------------------


@pytest.mark.parametrize(
    ("query", "substance", "basis", "value", "unit"),
    [
        # The exact lookup the live run answered from training instead.
        ("Pd", "Palladium (Pd)", "oral PDE", 100.0, "µg/day"),
        ("palladium", "Palladium (Pd)", "parenteral PDE", 10.0, "µg/day"),
        ("THF", "Tetrahydrofuran", "PDE", 7.2, "mg/day"),
        ("tetrahydrofuran", "Tetrahydrofuran", "concentration limit", 720.0, "ppm"),
        ("C1CCOC1", "Tetrahydrofuran", "PDE", 7.2, "mg/day"),  # resolved from a structure
        ("DMF", "N,N-Dimethylformamide", "PDE", 8.8, "mg/day"),
        ("2-MeTHF", "2-Methyltetrahydrofuran", "PDE", 5.0, "mg/day"),
        ("benzene", "Benzene", "concentration limit", 2.0, "ppm"),  # Class 1: a limit, no PDE
        # Class 3, reached via an abbreviation. Its basis is *not* "PDE": Q3C assigns Class 3 no
        # solvent-specific PDE, so quoting one under that label would attribute a number to the
        # guideline that it does not contain.
        (
            "IPA",
            "2-Propanol",
            "Class 3 general limit — Q3C assigns no solvent-specific PDE; 50 mg/day or more "
            "is acceptable without justification",
            50.0,
            "mg/day",
        ),
    ],
)
def test_a_transcribed_limit_comes_back_with_its_number(
    query: str, substance: str, basis: str, value: float, unit: str
) -> None:
    """The number is read off a vendored table, and the same substance answers to every spelling.

    A SMILES and an abbreviation both resolve through the reagent table, so a chemist does not have
    to know the guideline's own spelling to reach its row.
    """
    limit = impurity_limit(query).limit
    assert limit is not None and limit.substance == substance
    assert {(entry.basis, entry.value, entry.unit) for entry in limit.limits} >= {
        (basis, value, unit)
    }


def test_every_limit_names_the_guideline_its_revision_and_its_table() -> None:
    """A number without provenance is a recalled number wearing a citation's clothes.

    The whole point of transcribing these tables is that someone can open the source document at the
    right page; a citation naming only "ICH" would not let them.
    """
    solvent = impurity_limit("THF").limit
    element = impurity_limit("Pd").limit
    assert solvent is not None and element is not None
    assert solvent.citation == (
        "ICH Q3C(R9), Impurities: Guideline for Residual Solvents, ICH Step 4 (2024), Table 2"
    )
    assert element.citation == (
        "ICH Q3D(R2), Guideline for Elemental Impurities, ICH Step 4 (2022), Table A.2.1"
    )


def test_the_solvent_classes_are_carried_not_inferred() -> None:
    """Class membership is the other half of the Q3C answer, and no limit implies it."""
    for query, expected in (("benzene", "Class 1"), ("DCM", "Class 2"), ("DMSO", "Class 3")):
        limit = impurity_limit(query).limit
        assert limit is not None and limit.limit_class == expected


@pytest.mark.parametrize("query", ["nickel", "Ni", "tert-butyl alcohol", "water", "unobtainium"])
def test_a_miss_is_a_miss_and_says_what_it_does_not_mean(query: str) -> None:
    """The load-bearing half: an untranscribed substance returns nothing, and explains the nothing.

    Nickel and `tert`-butyl alcohol are the sharp cases — both are genuinely in a guideline, and
    both were left out of the transcription because their values could not be verified against the
    source. A miss that read as "no limit exists" would be worse than the fabrication this replaces.
    """
    lookup = impurity_limit(query)
    assert lookup.limit is None
    assert "not that no limit exists" in lookup.verdict
    assert "do not state one from memory" in lookup.verdict


def test_the_miss_verdict_is_serialized_not_merely_a_property() -> None:
    """The sentence has to reach the model writing the answer, which reads the payload."""
    assert "not that no limit exists" in impurity_limit("unobtainium").model_dump()["verdict"]
