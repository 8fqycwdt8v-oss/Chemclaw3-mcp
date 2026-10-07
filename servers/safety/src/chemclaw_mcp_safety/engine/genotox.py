"""Genotoxicity structural alerts — DNA-reactive motifs, never an ICH M7 classification.

A separate table from `screen.py` because the questions differ: process safety asks "is this safe to
run today", this asks "will this need a control strategy" (nitrobenzene passes one and is flagged by
the other). A structural alert is a motif and is encoded here; an ICH M7 class, a purge factor and
an acceptable intake come from (Q)SAR models and expert review this system does not have, so no
alert carries them. `AlertResult.verdict` says so on every result, hit or miss, because the payload
is what is in the context window when the answer is written.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, computed_field
from rdkit import Chem

from chemclaw_mcp_safety.engine.screen import (
    compile_smarts,
    parse_components,
    read_table,
    require_screenable_size,
)

__all__ = [
    "ALERTS_DIR",
    "ALERTS_FILE",
    "AlertResult",
    "AlertTable",
    "GenotoxAlert",
    "screen_genotoxic_alerts",
]

# The alert table, vendored and checksummed; not configurable, since a swapped table would change
# what the disclaimer is attached to.
ALERTS_DIR = Path(__file__).resolve().parent.parent / "data" / "genotox"
ALERTS_FILE = "genotox_alerts.yaml"

# Repeated on every result, hit or miss, naming exactly what the system cannot produce.
_NOT_A_CLASSIFICATION = (
    "An alert is a DNA-reactive structural motif requiring expert assessment. It is NOT an ICH M7 "
    "class, an acceptable intake, a purge factor, or a (Q)SAR prediction — this system has none of "
    "those and must not state one."
)


class GenotoxAlert(BaseModel):
    """One matched alert: which motif, why it is one, where the claim comes from, and what matched.

    No severity field, unlike `HazardFlag`. Ranking alerts would be the first half of a
    classification, and the published alert sets do not rank them either.
    """

    alert_id: str = Field(min_length=1)
    motif: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    citation: str = Field(min_length=1)
    # A SMILES for a structural alert, or "a + b" for a formation pair.
    matched: str = Field(min_length=1)


class AlertResult(BaseModel):
    """The alerts raised for one molecule or route, in table order, and what was screened.

    `screened` mirrors `ScreenResult.screened` exactly — the canonical SMILES of every structure
    this result covers, deduplicated, in the order given — and it is here for the same two reasons
    and one more. A clean result otherwise names nothing it looked at, which is worst on precisely
    this result type (`verdict` spends three lines saying an empty list is not a negative
    mutagenicity prediction, about molecules the payload never identifies); and a surface that must
    render a genotox alert *distinctly* from a general hazard needs both results to key on the same
    entity, which they cannot do while each carries only the caller's own spelling.
    """

    alerts: list[GenotoxAlert] = Field(default_factory=list)
    screened: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> str:
        """What a reader must be told about this result, present on a hit *and* on a miss.

        A miss is the dangerous case: "no alerts" must not read as "not mutagenic".
        """
        if not self.alerts:
            return (
                "No structural alert in the table matched. That is not a negative mutagenicity "
                f"prediction and not an ICH M7 classification — the table is a short, cited alert "
                f"list, not a (Q)SAR. {_NOT_A_CLASSIFICATION}"
            )
        motifs = ", ".join(alert.motif for alert in self.alerts)
        return f"{len(self.alerts)} structural alert(s) matched ({motifs}). {_NOT_A_CLASSIFICATION}"


class _Alert(BaseModel):
    """One structural alert as loaded from the table."""

    id: str = Field(min_length=1)
    smarts: str = Field(min_length=1)
    motif: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    citation: str = Field(min_length=1)


class _FormationPair(BaseModel):
    """One alert about two components meeting, rather than about a structure that is present."""

    id: str = Field(min_length=1)
    left: str = Field(min_length=1)
    right: str = Field(min_length=1)
    motif: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    citation: str = Field(min_length=1)


class AlertTable(BaseModel):
    """The parsed alert file: structural motifs plus formation routes.

    Public for the reason `RuleTable` is: `tests/test_dataset.py` validates the corpus against
    itself, and the alert table's citations are a property of the table rather than of a screen.
    """

    structural: list[_Alert] = Field(min_length=1)
    formation_pairs: list[_FormationPair] = Field(default_factory=list)


@lru_cache(maxsize=1)
def _load_alerts() -> tuple[AlertTable, dict[str, Chem.Mol]]:
    """Parse and compile the alert table once per process (it is a vendored, checksummed file).

    Patterns are keyed `<alert id>`, or `<pair id>:left` / `:right` for formation pairs, as in
    `screen.py`.
    """
    table = read_table(ALERTS_DIR, ALERTS_FILE, AlertTable)
    patterns = {alert.id: compile_smarts(alert.smarts, alert.id) for alert in table.structural}
    for pair in table.formation_pairs:
        patterns[f"{pair.id}:left"] = compile_smarts(pair.left, pair.id)
        patterns[f"{pair.id}:right"] = compile_smarts(pair.right, pair.id)
    return table, patterns


def screen_genotoxic_alerts(component_smiles: list[str]) -> AlertResult:
    """Match one molecule, or every component of a route, against the alert table.

    Pass the whole route: a formation pair is only visible among components given together.

    Args:
        component_smiles: One SMILES per species — the molecule alone, or every reactant, reagent,
        solvent and product whose meeting is being assessed.

    Raises:
        SafetyRulesError: A component does not parse in full (named by its position), the alert
        table is malformed, or the list is empty or longer than `MAX_COMPONENTS`.
    """
    require_screenable_size(component_smiles, what="a genotoxicity screen")
    table, patterns = _load_alerts()
    # `parse_components`, shared with the hazard screen, so a truncating parse cannot report an
    # empty alert list about a molecule the caller never named.
    molecules = parse_components(component_smiles)
    alerts = [
        GenotoxAlert(
            alert_id=alert.id,
            motif=alert.motif,
            explanation=alert.explanation,
            citation=alert.citation,
            matched=smiles,
        )
        for alert in table.structural
        for smiles, molecule in molecules.items()
        if molecule.HasSubstructMatch(patterns[alert.id])
    ]
    for pair in table.formation_pairs:
        left = [s for s, m in molecules.items() if m.HasSubstructMatch(patterns[f"{pair.id}:left"])]
        right = [
            s for s, m in molecules.items() if m.HasSubstructMatch(patterns[f"{pair.id}:right"])
        ]
        alerts.extend(
            GenotoxAlert(
                alert_id=pair.id,
                motif=pair.motif,
                explanation=pair.explanation,
                citation=pair.citation,
                matched=f"{a} + {b}",
            )
            for a in left
            for b in right
            if a != b
        )
    # Deduplicated after canonicalising, since `molecules` is keyed on the caller's spelling.
    canonical = list(dict.fromkeys(str(Chem.MolToSmiles(m)) for m in molecules.values()))
    return AlertResult(alerts=alerts, screened=canonical)
