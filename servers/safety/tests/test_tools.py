"""What the three tools answer, and what they refuse, offline against the real tables.

An advisory screen must fire on real examples of its motifs, stay quiet on ordinary chemistry,
and never render "no match" as "safe"; rules are pinned with named molecules, not a mocked
matcher. The genotoxicity alerts and ICH limits are tested here too, because what matters is how
they relate to the hazard screen: separate questions, none of them a classification.
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

    The checksum is computed, not pasted, so these tests are about the loader's behaviour on a
    broken table; the real corpus's checksum is asserted in `test_dataset.py`.
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
                "refresh_owner": "team:test-owners",
                "refresh_cadence": "P12M",
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
    """Polynitroarenes flag at every substitution pattern, not only ortho.

    A ring-chain SMARTS hanging the second nitro off the ring-closure atom matches ortho only, which
    let TNT and picric acid screen clean. A rule whose semantics are a count and relative positions
    needs one molecule per arrangement it claims, not one reference molecule.
    """
    assert "polynitro-aromatic" in {flag.rule_id for flag in screen_structure(smiles).flags}, name


def test_a_mononitroarene_is_not_polynitro() -> None:
    """One nitro group on a ring does not fire the polynitro rule.

    This makes the count real: `min_matches` wired as `>= 1` would pass every match above while
    turning the explosive alert into "contains a nitro group".
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
    """Every azide not bonded to carbon flags, not just the organic ones.

    `organic-azide` and `acyl-azide` open on carbon, so sodium azide, hydrazoic acid and the
    silyl/phosphoryl transfer reagents would otherwise screen clean; each is pinned by name.
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
    """A structure with trailing text is refused, not quietly narrowed to its prefix.

    RDKit parses a valid prefix and ignores what follows a space, which would give a clean screen of
    ethanol for an input whose tail is an azide. Asserted as a refusal and as the absence of a clean
    result, since pinning only the exception would pass a version that returned one.
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
    """A good call still echoes `screened`, canonical and deduplicated.

    `screened` is the evidence that a screen is about the molecules the caller meant.
    """
    result = screen_reaction(["OCC", "CCO", "O"])
    assert result.screened == ["CCO", "O"]


def test_a_screen_of_nothing_is_refused_rather_than_answered_cleanly() -> None:
    """A screen of nothing is refused rather than answered cleanly.

    An empty input would produce exactly a clean screen's payload with no molecule ever looked at;
    `screened` exists so a clean result names its subject.
    """
    for screen in (screen_reaction, screen_genotoxic_alerts):
        with pytest.raises(SafetyRulesError, match="at least one structure"):
            screen([])


def test_a_missing_rule_table_fails_loudly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing table stops the screen instead of silently reporting no hazards.

    The message names the file, because `read_table` is shared by the hazard, genotoxicity and ICH
    tables.
    """
    monkeypatch.setattr(screen_module, "RULES_DIR", tmp_path / "missing")
    with pytest.raises(SafetyRulesError, match=r"cannot read the safety table rules\.yaml"):
        screen_structure("CCO")


def test_a_rule_table_that_is_not_the_approved_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rule table that is not the approved file is refused by its checksum.

    A truncated or swapped table answers "no rule matched", indistinguishable from a clean molecule.
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

    Atom-map labels give variety at constant size; growing a chain would make the total atom count
    grow too and a timing curve would measure the input.
    """
    half = n // 2
    return [left.format(i=i + 1) for i in range(half)] + [
        right.format(i=i + 1) for i in range(n - half)
    ]


def test_a_reaction_screen_refuses_more_components_than_it_can_screen() -> None:
    """Pair rules are a cross-product, so an oversized component list is refused before matching.

    The amplification is in the response, so a request-size cap does not bound it. Raising
    `CHEMCLAW_SAFETY_MAX_COMPONENTS` fails this.
    """
    oversized = _distinct_pair_matching(MAX_COMPONENTS + 1, "[NH2:{i}]N", "[OH:{i}]O")
    with pytest.raises(SafetyRulesError, match="at most"):
        screen_reaction(oversized)


def test_a_genotoxicity_screen_refuses_the_same_way() -> None:
    """The genotoxicity screen has the same cross-product shape and refuses the same way.

    One bound for both callers; refusing in one place only would just move the defect.
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
    """The "not a safety assessment" line survives `model_dump()`.

    A bare `property` is dropped by serialization, so the assertion is on the dumped payload;
    reading `result.verdict` would pass either way.
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
    """A textbook member of a covered hazard class does not screen clean.

    Sodium peroxide (anionic one-coordinate oxygens), UDMH (H on one nitrogen) and chloramine-T
    (anionic two-coordinate nitrogen) each fall outside a pattern written for the neutral case.
    """
    assert rule in {flag.rule_id for flag in screen_structure(smiles).flags}, name


def test_a_peroxide_salt_is_an_oxidizer_to_the_pair_rule_as_well() -> None:
    """A peroxide salt is an oxidiser to the pair rule as well as to `peroxide`.

    `Na2O2 + NaBH4`, a strong oxidiser with a complex hydride, is the case the pair rule is named
    for.
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
    """A hydrazinium salt is a hydrazine to both rules.

    Protonation makes the nitrogen `NX4+`, and the hydrochloride or sulfate is the ordinary
    catalogue form, so both the structural and the pair rule must match it.
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


# A 1,1-disubstituted hydrazine beside an oxidiser, as (name, SMILES, rule). UDMH with H2O2 or
# N2O4 is the archetypal hypergolic pair: it ignites on contact.
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
    """A disubstituted hydrazine is a reductant to the pair rule, as to its structural twin.

    The pair arm must not require H on both nitrogens, or UDMH plus peroxide would miss the rule
    named for it.
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
    """A diarylhydrazine is still a hydrazine.

    An aryl guard meant for azo systems only changes the verdict when both nitrogens are aryl-bound
    (otherwise the match is found from the other side), so hydrazobenzene is the molecule that
    shows it.
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


# The false-positive half of the widened hydrazine pattern: each carries a nitrogen it could
# plausibly reach (second N, cation, N-O bond, aryl amine, acylated N-N) and none is a hydrazine.
# A pattern that cries wolf gets the whole screen switched off.
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
    """Neither hydrazine rule fires on a molecule that merely contains nitrogen."""
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
    """Every structural alert has a worked example and a counterexample.

    Parametrised from the table itself, so a row added without a molecule that must match fails here
    rather than hiding a claimed-but-unencoded motif.
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
    """A vinyl sulfone raises an alkylating alert.

    `michael-acceptor` needs a conjugated carbonyl, so a vinyl sulfone warhead would screen clean
    while the alert's explanation claims it; a miss on a named motif reads as a pass.
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
    """A nitrosating agent beside an amine flags the nitrosamine formation route.

    Neither component is an alert alone (DIPEA, sodium nitrite), so this is a pair rule over the
    component list.
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
    """The disclaimer rides in the payload, on a hit and on a miss.

    `ScreenResult.verdict` is a `computed_field` so it is serialized; the four things this system
    cannot produce are named individually.
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
    """The genotoxicity table does not leak into the process-safety screen, or vice versa.

    Nitrobenzene shows both directions: the hazard table rightly passes it and the alert table
    rightly flags it.
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
    """A component with trailing text stops the alert screen too.

    Otherwise the nitroarene after the space would be dropped and an empty alert list returned about
    a molecule the payload never identifies.
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
    """An untranscribed substance returns nothing, and explains the nothing.

    Nickel and tert-butyl alcohol are in a guideline but omitted as unverified; a miss reading as
    "no limit exists" would be worse than a fabricated value.
    """
    lookup = impurity_limit(query)
    assert lookup.limit is None
    assert "not that no limit exists" in lookup.verdict
    assert "do not state one from memory" in lookup.verdict


def test_the_miss_verdict_is_serialized_not_merely_a_property() -> None:
    """The sentence has to reach the model writing the answer, which reads the payload."""
    assert "not that no limit exists" in impurity_limit("unobtainium").model_dump()["verdict"]
