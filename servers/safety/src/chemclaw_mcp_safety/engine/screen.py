"""Deterministic structural hazard screening (Chemclaw3 D-080) — advisory flags, never a clearance.

SMARTS matching against a cited rule table (`data/rules/rules.yaml`) plus a pairwise incompatibility
check across a reaction's components: reproducible, offline, traceable to a source. No match means
only that *no rule in the table matched*; `ScreenResult.verdict` never says "safe", because an
over-trusted screen is worse than none. The table is a vendored, checksummed corpus rather than a
setting; extending it is a reviewed pull request.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Literal, TypeVar

import yaml
from mcp_server_kit import DatasetError, load_dataset
from mcp_server_kit.limits import env_bound
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

# The vendored rule table; a directory because `load_dataset` verifies it against the `dataset.json`
# beside it.
RULES_DIR = Path(__file__).resolve().parent.parent / "data" / "rules"
RULES_FILE = "rules.yaml"

# The most components one screen may carry.
#
# Pair rules are checked as a cross-product, so output grows with the square of a tiny request; a
# body-size cap does not bound it. 64 is far above any real reaction and keeps the worst case to
# about a thousand pair flags.
MAX_COMPONENTS = env_bound(
    "CHEMCLAW_SAFETY_MAX_COMPONENTS",
    default=64,
    # `0` would refuse every screen on a pod that reports itself ready.
    minimum=1,
    consequence="every hazard screen would be refused, on a pod that starts and passes readiness",
)

Severity = Literal["high", "medium", "low"]

# Ordered worst-first: used to rank flags.
_SEVERITY_ORDER: dict[str, int] = {"high": 3, "medium": 2, "low": 1}


class SafetyRulesError(ValueError):
    """A screen cannot be performed: the input is unusable, or a rule table is missing/malformed.

    Fatal rather than skip-and-continue: half a rule table would report "no rule matched" for a
    covered hazard. A `ValueError` so `connector_app` passes the chemist-facing message to the model
    verbatim.
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

        A `computed_field` so it is serialised: the caveat must be in the result payload, which is
        what is in the context window when the answer is written.
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
    # Counts ("two or more nitro groups") cannot be expressed in one SMARTS without enumerating
    # every arrangement, so the count lives beside the pattern. Counted with `GetSubstructMatches`
    # (uniquified) and not the `maxMatches` short-circuit, which caps raw embeddings and could
    # under-count a symmetric pattern into a false negative.
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

    The one loader for the hazard rules, genotoxicity alerts and both ICH tables. A table that
    cannot be read stops the answer: an empty rule set would read as a clean molecule and an empty
    ICH index as an honest miss. The checksum catches a truncated or swapped file. The message names
    the file, so a genotox fault is not reported as a hazard-rule fault.

    Raises:
        SafetyRulesError: The corpus is missing, unapproved, not a mapping, or does not validate
            into `model`.
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

    Public so the genotoxicity table shares the behaviour.
    """
    pattern = Chem.MolFromSmarts(smarts)
    if pattern is None:
        raise SafetyRulesError(f"hazard rule {rule_id!r} has unparseable SMARTS: {smarts!r}")
    return pattern


@lru_cache(maxsize=4)
def _load_rules(directory: Path) -> tuple[RuleTable, dict[str, Chem.Mol]]:
    """Parse and compile the rule table in `directory` (cached — it is a vendored file).

    Patterns are keyed `<rule id>`, or `<rule id>:left` / `:right` for pair rules, and compiled once
    per process.
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

    A bare `Chem.MolFromSmiles` drops everything after a space, so `"CCO CN=[N+]=[N-]"` would screen
    clean as ethanol; `chem.require_molecule` refuses it. `InvalidSmilesError` becomes
    `SafetyRulesError` so the package raises one type (both are `ValueError`s). `subject` names what
    could not be read, e.g. `"component 4 of 9"` from `parse_components`. Public so the genotoxicity
    screen fails identically.
    """
    try:
        return require_molecule(smiles)
    except InvalidSmilesError as exc:
        raise SafetyRulesError(f"cannot screen {subject}: {exc}") from exc


def parse_components(component_smiles: Sequence[str]) -> dict[str, Chem.Mol]:
    """Parse every component of a reaction or route, keyed by the caller's own spelling.

    Shared by both screens so they refuse identically. A refusal names the component's 1-based
    position in the caller's list (not in the deduplicated mapping). Keyed on the caller's spelling
    because flags report those strings; `screened` echoes the canonical form.
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

    Refused, never truncated: a dropped component would yield "no rule matched" for chemistry never
    looked at. An empty list is refused for the same reason — a clean screen of nothing reads as a
    clearance. Public so both screens refuse identically.

    Raises:
        SafetyRulesError: No components were given, or more than `MAX_COMPONENTS` were.
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
        SafetyRulesError: The SMILES does not parse in full, or the rule table is missing/malformed.
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
    # Canonicalised from the molecule already parsed, rather than re-parsing the string.
    return ScreenResult(flags=_sorted(flags), screened=[str(Chem.MolToSmiles(molecule))])


def screen_reaction(component_smiles: list[str]) -> ScreenResult:
    """Screen every component of a reaction, plus incompatibilities *between* components.

    Pair rules catch combinations no per-molecule screen can see (an oxidiser with a reducing
    agent). Structural flags are deduplicated per (rule, molecule).

    Args:
        component_smiles: Every species in the reaction (reactants, reagents, solvents, products).

    Raises:
        SafetyRulesError: A component does not parse in full (named by position), the rule table is
            missing/malformed, or the list is empty or longer than `MAX_COMPONENTS`.
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
    # Deduplicated again after canonicalising: `CCO` and `OCC` are one substance written twice.
    canonical = list(dict.fromkeys(str(Chem.MolToSmiles(m)) for m in molecules.values()))
    return ScreenResult(flags=_sorted(flags), screened=canonical)
