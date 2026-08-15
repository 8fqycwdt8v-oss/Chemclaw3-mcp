"""Deterministic structural hazard screening (Chemclaw3 D-080) — advisory flags, never a clearance.

What this is: SMARTS matching against a committed, cited rule table (`data/rules/rules.yaml`), plus
a pairwise incompatibility check across a reaction's components. Deterministic, offline, no model,
no external database — a flag is reproducible and traceable to a literature source, which is what
makes it usable in a review.

**What this is not, and must never be presented as:** a hazard assessment. No rule matching means
*no rule in the table matched* — it says nothing about toxicity, exposure, thermal stability of the
specific compound, scale, or the process around it. `ScreenResult.verdict` deliberately renders that
as "no rule matched" rather than any word resembling "safe": an over-trusted screen is more
dangerous than no screen, because it converts an absence of knowledge into apparent assurance.

**The rule table is baked into the image and is no longer a setting.** Chemclaw3 carried its path as
`settings.safety_rules_path` so a site could point at its own table. Here it is a vendored corpus
with a licence, a checksum and a `dataset.json` a reviewer signed off on, and the checksum is the
point: a swapped-in table would be a different set of claims wearing the same citations. Extending
the table is a pull request against this file, which is where a process-safety chemist's addition
gets reviewed anyway.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Literal, TypeVar

import yaml
from mcp_server_kit import DatasetError, load_dataset
from pydantic import BaseModel, Field, computed_field
from rdkit import Chem

from chemclaw_mcp_safety.engine.chem import InvalidSmilesError, require_molecule

__all__ = [
    "MAX_COMPONENTS",
    "RULES_DIR",
    "RULES_FILE",
    "HazardFlag",
    "RuleTable",
    "SafetyRulesError",
    "ScreenResult",
    "Severity",
    "compile_smarts",
    "parse_components",
    "parse_molecule",
    "read_table",
    "require_screenable_size",
    "screen_reaction",
    "screen_structure",
]

# The vendored rule table. A directory rather than a file because `load_dataset` verifies the
# corpus against the `dataset.json` beside it, and a rule table nobody can checksum is a rule table
# nobody can show was the one that was reviewed.
RULES_DIR = Path(__file__).resolve().parent.parent / "data" / "rules"
RULES_FILE = "rules.yaml"

# The most components one screen may carry.
#
# Both screens in this package check their pair rules as a *cross-product*, so the flags they can
# produce grow with the square of the input while the request itself stays tiny: 13 KiB of SMILES
# was measured in Chemclaw3 producing 251,000 hazard flags and blocking the serving connector's
# event loop for 2.48 s, and the genotoxicity screen has the same shape (640 components, 102,400
# alerts, 933 ms). A request-size cap is no bound on this, because the amplification is in the
# response.
#
# 64 is far above any real reaction — the largest shipped ELN entry has well under a dozen species —
# and bounds the worst case to ~1,000 pair flags and single-digit milliseconds. Chemclaw3 carried it
# as `settings.safety_max_components`; here it is one environment variable at the same default,
# because one integer does not earn a pydantic-settings dependency.
MAX_COMPONENTS = int(os.environ.get("CHEMCLAW_SAFETY_MAX_COMPONENTS", "64"))

Severity = Literal["high", "medium", "low"]

# Ordered worst-first: used to rank flags.
_SEVERITY_ORDER: dict[str, int] = {"high": 3, "medium": 2, "low": 1}


class SafetyRulesError(ValueError):
    """A screen cannot be performed: the input is unusable, or a rule table is missing/malformed.

    Fatal rather than skip-and-continue: silently screening with half a rule table would report
    "no rule matched" for a hazard the table covers — the one failure mode this package exists to
    prevent.

    A `ValueError` for the reason `InvalidSmilesError` is one: `mcp_server_kit.connector_app` lets a
    `ValueError` reach the model verbatim and replaces every other exception with a generic notice.
    Every message this type carries is written for the chemist — which structure was refused, which
    component of the list it was, which table could not be read — and a refusal the model cannot
    read is a refusal it will report as a result.
    """


class HazardFlag(BaseModel):
    """One matched hazard rule: what fired, how serious, why, and where the claim comes from."""

    rule_id: str = Field(min_length=1)
    severity: Severity
    explanation: str = Field(min_length=1)
    citation: str = Field(min_length=1)
    # Which input the rule matched — a SMILES for a structural rule, or "a + b" for a pair rule.
    matched: str = Field(min_length=1)


class ScreenResult(BaseModel):
    """The flags raised for one molecule or reaction, worst first, and what was screened.

    `screened` is the **canonical** SMILES of every structure this result covers, in the order they
    were given and deduplicated, taken off the molecule the screen has already parsed rather than by
    parsing a second time.

    Two things it fixes, and neither is cosmetic. First, a clean screen used to serialize to
    `{"flags": [], "verdict": …}` — nothing in the payload said *what* had been screened, so "no
    rule matched" arrived with no subject, which for a result whose whole discipline is that it must
    never read as a clearance is the wrong thing to be vague about. Second, it gives a consumer a
    stable entity key: `COc1ccc(Br)cc1` and `BrC1=CC=C(OC)C=C1` are one molecule and two strings,
    and a surface holding only the caller's spelling cannot know that.

    Deliberately *not* used to rewrite `HazardFlag.matched`, which stays the caller's own spelling:
    `matched` answers "which input did this rule fire on", and for a pair rule it is `"a + b"`
    rather than a structure at all.
    """

    flags: list[HazardFlag] = Field(default_factory=list)
    screened: list[str] = Field(default_factory=list)

    @property
    def max_severity(self) -> Severity | None:
        """The most serious severity present, or None when nothing matched."""
        return self.flags[0].severity if self.flags else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> str:
        """A one-line summary for a human — never the word "safe" (see the module docstring).

        `computed_field`, not a bare `property`, and the difference is the whole point of the
        sentence. A plain property is not serialized: `model_dump()` on a clean screen returned
        exactly `{"flags": []}`, so the disclaimer had **zero** production callers and never reached
        the model that had to write the answer. A live run then showed a chemist saying they wanted
        to sign a risk assessment being told "no hazards detected" six times — the precise phrasing
        the safety-screening judgment forbids in bold.

        The tool docstring already said all of this. A docstring is read once when the tool is
        defined; the result payload is what is in the context window when the answer is written, and
        only one of those two was carrying the caveat.
        """
        if not self.flags:
            return "No rule in the hazard table matched. This is not a safety assessment."
        return (
            f"{len(self.flags)} hazard rule(s) matched (most serious: {self.max_severity}). "
            "Advisory only — a human must assess the procedure."
        )


class _StructuralRule(BaseModel):
    """One structural alert as loaded from the rule table."""

    id: str = Field(min_length=1)
    smarts: str = Field(min_length=1)
    severity: Severity
    explanation: str = Field(min_length=1)
    citation: str = Field(min_length=1)
    # How many *distinct* matches of `smarts` a molecule must contain before the rule fires.
    #
    # A count is not expressible as a substructure boolean: "polynitro" means "two or more nitro
    # groups", and SMARTS can only say "this arrangement is present", so a single pattern has to
    # enumerate every relative arrangement — ortho, meta, para, then every ring size, then every
    # fused system. `polynitro-aromatic` tried to inline the count into the pattern by spelling the
    # ring out, and therefore matched *only* 1,2-dinitroarenes: TNT and picric acid screened clean.
    # There is no pattern-only fix; the count has to live beside the pattern.
    #
    # Counted with `GetSubstructMatches` at its default `uniquify=True`, and deliberately *not* with
    # RDKit's `maxMatches` short-circuit: `maxMatches` caps the raw embeddings collected before
    # uniquification, so a symmetric pattern (`[OX2][OX2]` embeds into HOOH twice, once each way)
    # could be truncated to fewer unique matches than the molecule really has. That would be a
    # silent false negative, which is the one failure mode this module exists to prevent.
    min_matches: int = Field(default=1, ge=1)


class _PairRule(BaseModel):
    """One incompatibility between two components of the same reaction."""

    id: str = Field(min_length=1)
    left: str = Field(min_length=1)
    right: str = Field(min_length=1)
    severity: Severity
    explanation: str = Field(min_length=1)
    citation: str = Field(min_length=1)


class RuleTable(BaseModel):
    """The parsed rule file: structural alerts plus pairwise incompatibilities.

    Public so `tests/test_dataset.py` can validate the corpus against itself — the two hydrazine
    patterns being the same string is a property of the *table*, and a test that had to reach for a
    private name to state it would be a test nobody writes.
    """

    structural: list[_StructuralRule] = Field(default_factory=list)
    incompatible_pairs: list[_PairRule] = Field(default_factory=list)


_Table = TypeVar("_Table", bound=BaseModel)


def read_table(directory: Path, records_file: str, model: type[_Table]) -> _Table:
    """Read one vendored YAML corpus, verify it against its `dataset.json`, and validate `model`.

    The one loader for all four corpora this server answers from — the hazard rules, the
    genotoxicity alerts and the two ICH tables — and generic over the model for that reason. They
    share their failure modes and the required response to them: a table that cannot be read must
    stop the answer rather than yield an empty one, because an empty rule set reports "nothing
    matched" (indistinguishable from a clean molecule) and an empty ICH index reports "this system
    does not carry the number" for a substance it does.

    The checksum is why this goes through `load_dataset` rather than straight to `yaml.safe_load`:
    a truncated COPY or a swapped file would otherwise be a *shorter rule table*, which is silent by
    construction.

    The message names the file rather than calling every table "hazard rules". This package works
    hard to keep the process-safety screen and the genotoxicity screen distinct — they answer
    different questions and one must never be reported as the other — and a malformed
    `genotox_alerts.yaml` announcing itself as a hazard-rule fault sends the reader to the wrong
    table.

    Raises:
        SafetyRulesError: the corpus is missing, is not the file its manifest approved, is not a
            mapping, or does not validate into `model`.
    """
    path = directory / records_file
    try:
        corpus = load_dataset(directory, records_file=records_file)
        raw = yaml.safe_load(corpus.records_path.read_text(encoding="utf-8"))
    except (DatasetError, OSError, yaml.YAMLError) as exc:
        raise SafetyRulesError(
            f"cannot read the safety table {records_file} at {path}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise SafetyRulesError(
            f"the safety table {records_file} at {path} must be a mapping, got {type(raw).__name__}"
        )
    try:
        return model.model_validate(raw)
    except ValueError as exc:
        raise SafetyRulesError(f"invalid safety table {records_file} at {path}: {exc}") from exc


def compile_smarts(smarts: str, rule_id: str) -> Chem.Mol:
    """Compile one rule's SMARTS, failing loudly with the rule id that owns it.

    Public for the same reason `read_table` is: the genotoxicity alert table needs the
    identical "name the rule that owns the broken pattern" behaviour, and two copies of it would
    drift.
    """
    pattern = Chem.MolFromSmarts(smarts)
    if pattern is None:
        raise SafetyRulesError(f"hazard rule {rule_id!r} has unparseable SMARTS: {smarts!r}")
    return pattern


@lru_cache(maxsize=4)
def _load_rules(directory: Path) -> tuple[RuleTable, dict[str, Chem.Mol]]:
    """Parse and compile the rule table in `directory` (cached — it is a vendored file).

    Returns the table and a pattern map keyed by `<rule id>` for structural rules and
    `<rule id>:left` / `:right` for pair rules, so every SMARTS is compiled exactly once per process
    rather than on every screened molecule.
    """
    table = read_table(directory, RULES_FILE, RuleTable)
    if not table.structural and not table.incompatible_pairs:
        raise SafetyRulesError(f"the hazard rules in {directory} contain no rules")
    patterns = {rule.id: compile_smarts(rule.smarts, rule.id) for rule in table.structural}
    for pair in table.incompatible_pairs:
        patterns[f"{pair.id}:left"] = compile_smarts(pair.left, pair.id)
        patterns[f"{pair.id}:right"] = compile_smarts(pair.right, pair.id)
    return table, patterns


def parse_molecule(smiles: str, *, subject: str = "the structure given") -> Chem.Mol:
    """Parse a SMILES **in full**, raising this package's error type so a caller handles one.

    Public because the genotoxicity alert screen must fail the same way on the same input; a second
    parser there would be a second place for "unparseable" to mean "clean".

    **In full is the word that was missing, and its absence was the defect.** A bare
    `Chem.MolFromSmiles` accepts a valid *prefix* and drops whatever follows a space — so
    `screen_hazards("CCO junk")` did not fail, it screened ethanol, reported "No rule in the hazard
    table matched", and echoed `CCO` in `screened` as the structure it had looked at. A concatenated
    or mistyped string therefore came back as a **clean screen of a different, smaller molecule**,
    which is the single worst outcome available to a tool whose entire documented discipline is that
    its empty result must never read as a clearance. Measured on the Chemclaw3 build:
    `"CCO CN=[N+]=[N-]"` — an azide sitting in the ignored tail — screened with zero flags.

    `InvalidSmilesError` is translated to `SafetyRulesError` so this package keeps its promise of
    raising one exception type; both are `ValueError`s, so either way the refusal reaches the model
    as a worded message rather than as an internal-error notice.

    `subject` names *what* could not be read, and it exists for the reaction path: a chemist handed
    "one of the nine components you gave me is unusable" cannot act on it, and `screen_reaction`
    passes `"component 4 of 9"` (see `parse_components`).
    """
    try:
        return require_molecule(smiles)
    except InvalidSmilesError as exc:
        raise SafetyRulesError(f"cannot screen {subject}: {exc}") from exc


def parse_components(component_smiles: Sequence[str]) -> dict[str, Chem.Mol]:
    """Parse every component of a reaction or route, keyed by the caller's own spelling.

    Shared by both screens for the reason `require_screenable_size` is: they must accept and refuse
    identical input identically, and a refusal that does not say *which* component failed leaves a
    chemist re-reading a list of nine SMILES to find the one with a stray space in it.

    The position reported is the component's place in the list as the caller wrote it, counted from
    1 — not its place in the deduplicated mapping, which is a different number the moment a reagent
    is listed twice.

    Keyed on the caller's spelling rather than the canonical form because `HazardFlag.matched` and
    the pair rules both report the strings the caller used; `screened` is where the canonical form
    is echoed, and it is derived from these molecules.
    """
    molecules: dict[str, Chem.Mol] = {}
    for position, smiles in enumerate(component_smiles, start=1):
        if smiles in molecules:
            continue
        molecules[smiles] = parse_molecule(
            smiles, subject=f"component {position} of {len(component_smiles)}"
        )
    return molecules


def require_screenable_size(component_smiles: list[str], *, what: str) -> None:
    """Refuse a component list this package cannot honestly screen — too large, or empty.

    Public because both screens must refuse identically. Refused rather than truncated: a hazard
    screen that silently dropped components would report "no rule matched" for chemistry it never
    looked at, and every tool description in this package says an empty result means no rule
    matched — never that something is safe. See `MAX_COMPONENTS` for the measured amplification the
    upper bound exists to stop.

    **The empty list is refused by that same sentence, one step further on.** `screen_hazards([])`
    answered `{"flags": [], "screened": [], "verdict": "No rule in the hazard table matched…"}` — a
    clean screen of *nothing*, the shape a model is most likely to paraphrase as "I screened it and
    it came back clear". This module's whole discipline is that an empty result must never read as a
    clearance, and an empty result that is not even about a molecule is the version of that with
    nothing in the payload to catch it.

    Raises:
        SafetyRulesError: no components were given, or more than `MAX_COMPONENTS` were.
    """
    if not component_smiles:
        raise SafetyRulesError(
            f"{what} needs at least one structure, and none were given. An empty result from this "
            "package means no rule matched the structures screened — with nothing screened there "
            "is no such statement to make, and it must not be reported as one."
        )
    if len(component_smiles) > MAX_COMPONENTS:
        raise SafetyRulesError(
            f"{what} accepts at most {MAX_COMPONENTS} components, got {len(component_smiles)}. "
            "Screen a reaction's own species, not a library: pair rules are checked between "
            "every pair, so the work grows with the square of the list."
        )


def _sorted(flags: list[HazardFlag]) -> list[HazardFlag]:
    """Worst severity first, then by rule id, so a result is deterministic and reads top-down."""
    return sorted(flags, key=lambda f: (-_SEVERITY_ORDER[f.severity], f.rule_id))


def screen_structure(smiles: str) -> ScreenResult:
    """Flag hazardous structural motifs in one molecule (advisory — see the module docstring).

    Raises:
        SafetyRulesError: the SMILES does not parse in full (see `parse_molecule` — a valid prefix
            with trailing text is refused, not screened), or the rule table is missing/malformed.
    """
    molecule = parse_molecule(smiles)
    table, patterns = _load_rules(RULES_DIR)
    flags = [
        HazardFlag(
            rule_id=rule.id,
            severity=rule.severity,
            explanation=rule.explanation,
            citation=rule.citation,
            matched=smiles,
        )
        for rule in table.structural
        if len(molecule.GetSubstructMatches(patterns[rule.id])) >= rule.min_matches
    ]
    # `Chem.MolToSmiles` on the molecule already in hand *is* what `require_canonical_smiles`
    # returns for this input — canonicalizing through the string would parse the same SMILES a
    # second time and raise a second exception type for an input `parse_molecule` has already
    # accepted.
    return ScreenResult(flags=_sorted(flags), screened=[str(Chem.MolToSmiles(molecule))])


def screen_reaction(component_smiles: list[str]) -> ScreenResult:
    """Screen every component of a reaction, plus incompatibilities *between* components.

    A reaction is more than its parts: an oxidizer and a reducing agent are each unremarkable alone
    and dangerous together, which no per-molecule screen can see. Structural flags from the
    components are deduplicated per (rule, molecule) so a reagent listed twice is reported once.

    Args:
        component_smiles: Every species in the reaction (reactants, reagents, solvents, products).

    Raises:
        SafetyRulesError: any component does not parse in full — the refusal names the component's
            position in the list given — the rule table is missing/malformed, or the list is empty
            or longer than `MAX_COMPONENTS`.
    """
    require_screenable_size(component_smiles, what="a hazard screen")
    table, patterns = _load_rules(RULES_DIR)
    molecules = parse_components(component_smiles)
    flags = [flag for smiles in molecules for flag in screen_structure(smiles).flags]
    for pair in table.incompatible_pairs:
        left = [s for s, m in molecules.items() if m.HasSubstructMatch(patterns[f"{pair.id}:left"])]
        right = [
            s for s, m in molecules.items() if m.HasSubstructMatch(patterns[f"{pair.id}:right"])
        ]
        matches = [(a, b) for a in left for b in right if a != b]
        flags.extend(
            HazardFlag(
                rule_id=pair.id,
                severity=pair.severity,
                explanation=pair.explanation,
                citation=pair.citation,
                matched=f"{a} + {b}",
            )
            for a, b in matches
        )
    # Deduplicated *again* after canonicalizing, because `molecules` is keyed on the caller's
    # spelling: a reaction listing `CCO` and `OCC` is one substance written twice, and a component
    # list a surface treats as entities must not show it as two.
    canonical = list(dict.fromkeys(str(Chem.MolToSmiles(m)) for m in molecules.values()))
    return ScreenResult(flags=_sorted(flags), screened=canonical)
