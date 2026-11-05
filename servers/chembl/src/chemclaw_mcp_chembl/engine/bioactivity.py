"""The aggregated bioactivities of one compound: per target, standard type and unit.

A row is a summary computed when the subset was built, over ChEMBL activities with an exact value
(`standard_relation` '='): their count, median, minimum and maximum in the unit ChEMBL standardised
to, the number of assays they came from, the median pChEMBL where ChEMBL computed one, and the
range of ChEMBL's assay confidence scores. Censored values ('>', '<') are counted, never averaged.
Rows with different units are never merged.

Invariants: exactly one compound is summarised per call (an identifier that matches several is
refused, naming them); every result carries the attribution and the caveat; nothing is estimated
for a target or type the subset does not hold.
"""

from __future__ import annotations

import sqlite3

from mcp_server_kit.limits import echo
from pydantic import BaseModel, Field

from chemclaw_mcp_chembl.engine import store
from chemclaw_mcp_chembl.engine.attribution import CAVEAT, Attribution, attribution, source_line
from chemclaw_mcp_chembl.engine.lookup import Kind, MatchedBy, MatchKind, resolve

__all__ = ["MAX_ROWS", "ActivityRow", "BioactivitySummary", "bioactivity_summary"]

#: Rows one call returns, most-measured first; a well-studied drug has hundreds.
MAX_ROWS = 200
DEFAULT_ROWS = 50
#: ChEMBL's assay confidence score runs 0 to 9 (9 = direct single-protein target assignment).
CONFIDENCE_RANGE = (0, 9)
#: Longest `standard_type` or target id accepted.
MAX_FILTER_CHARS = 64


class ActivityRow(BaseModel):
    """One aggregate: a compound's measurements of one standard type against one target."""

    target_chembl_id: str
    target_name: str | None
    target_type: str | None = Field(description="e.g. 'SINGLE PROTEIN', 'CELL-LINE', 'ORGANISM'.")
    organism: str | None
    standard_type: str = Field(description="ChEMBL's standardised measure: IC50, Ki, Kd, EC50, ...")
    units: str | None = Field(
        description="ChEMBL's standard unit for this type, e.g. 'nM'; null when unitless. "
        "Rows of different units are never merged."
    )
    n_exact_values: int = Field(
        description="Measurements with an exact value ('=') — what the "
        "median, minimum and maximum summarise."
    )
    n_censored_values: int = Field(
        description="Measurements reported as '>' or '<' (a bound, "
        "not a value); counted, not averaged."
    )
    n_assays: int = Field(description="Distinct ChEMBL assays the measurements came from.")
    median: float | None = Field(
        description="Median of the exact values, in `units`; null if none."
    )
    minimum: float | None = Field(description="Smallest exact value, in `units`.")
    maximum: float | None = Field(description="Largest exact value, in `units`.")
    median_pchembl: float | None = Field(
        description="Median pChEMBL (-log10 of a molar IC50, Ki, Kd, EC50 ...) over exact values "
        "ChEMBL gave one; unitless, higher is more potent; null where ChEMBL computed none."
    )
    assay_confidence_min: int | None = Field(
        description="Lowest ChEMBL assay confidence score (0-9)."
    )
    assay_confidence_max: int | None = Field(
        description="Highest ChEMBL assay confidence score (0-9; 9 = direct single-protein target)."
    )


class BioactivitySummary(BaseModel):
    """The summaries for one compound, the filters applied, and the notice for every answer."""

    query: str
    matched_by: MatchedBy | None
    match_kind: MatchKind | None
    found: bool
    chembl_id: str | None
    pref_name: str | None
    rows: list[ActivityRow]
    rows_matching: int = Field(description="Rows that pass the filters, before the row limit.")
    truncated: bool = Field(description="True when more rows pass the filters than are returned.")
    filters: dict[str, str | int | None]
    note: str | None
    source: str
    caveat: str
    attribution: Attribution


def _validated_filters(
    standard_type: str | None,
    target_chembl_id: str | None,
    min_confidence_score: int | None,
    limit: int,
) -> None:
    for label, value in (("standard_type", standard_type), ("target_chembl_id", target_chembl_id)):
        if value is not None and (not value.strip() or len(value) > MAX_FILTER_CHARS):
            raise ValueError(f"{label} must be 1 to {MAX_FILTER_CHARS} characters")
    low, high = CONFIDENCE_RANGE
    if min_confidence_score is not None and not low <= min_confidence_score <= high:
        raise ValueError(
            f"min_confidence_score is ChEMBL's {low}-{high} scale, got {min_confidence_score}"
        )
    if not 1 <= limit <= MAX_ROWS:
        raise ValueError(f"limit must be between 1 and {MAX_ROWS}, got {limit}")


def _ambiguity(connection: sqlite3.Connection, ids: tuple[str, ...], truncated: bool) -> str:
    named = []
    for chembl_id in ids[:10]:
        row = connection.execute(
            "SELECT pref_name FROM compound WHERE chembl_id = ?", (chembl_id,)
        ).fetchone()
        named.append(f"{chembl_id} ({row['pref_name']})" if row["pref_name"] else chembl_id)
    more = " and more" if truncated or len(ids) > 10 else ""
    return (
        f"that identifier matches {len(ids)} compounds{more}: {', '.join(named)}. "
        "Summarise one at a time by passing its ChEMBL id."
    )


def bioactivity_summary(
    identifier: str,
    kind: Kind = "auto",
    standard_type: str | None = None,
    target_chembl_id: str | None = None,
    min_confidence_score: int | None = None,
    limit: int = DEFAULT_ROWS,
) -> BioactivitySummary:
    """Summarise one compound's activities; see the module docstring for what a row is."""
    _validated_filters(standard_type, target_chembl_id, min_confidence_score, limit)
    where = ["a.compound_chembl_id = ?"]
    params: list[str | int] = []
    if standard_type is not None:
        where.append("a.standard_type = ? COLLATE NOCASE")
        params.append(standard_type.strip())
    if target_chembl_id is not None:
        where.append("a.target_chembl_id = ? COLLATE NOCASE")
        params.append(target_chembl_id.strip())
    if min_confidence_score is not None:
        where.append("a.assay_confidence_max >= ?")
        params.append(min_confidence_score)
    clause = " AND ".join(where)
    filters: dict[str, str | int | None] = {
        "standard_type": standard_type,
        "target_chembl_id": target_chembl_id,
        "min_confidence_score": min_confidence_score,
    }
    with store.connect() as connection:
        resolved = resolve(connection, identifier, kind)
        if len(resolved.ids) > 1:
            raise ValueError(_ambiguity(connection, resolved.ids, resolved.truncated))
        chembl_id = resolved.ids[0] if resolved.ids else None
        rows: list[ActivityRow] = []
        total = 0
        pref_name = None
        if chembl_id is not None:
            pref_name = connection.execute(
                "SELECT pref_name FROM compound WHERE chembl_id = ?", (chembl_id,)
            ).fetchone()["pref_name"]
            total = connection.execute(
                f"SELECT COUNT(*) FROM activity_summary a WHERE {clause}",  # noqa: S608 - fixed clauses, bound params
                (chembl_id, *params),
            ).fetchone()[0]
            for row in connection.execute(
                "SELECT a.target_chembl_id, t.pref_name AS target_name, t.target_type, t.organism, "  # noqa: S608
                "a.standard_type, a.standard_units, a.n_exact, a.n_censored, a.n_assays, "
                "a.median, a.minimum, a.maximum, a.median_pchembl, a.assay_confidence_min, "
                "a.assay_confidence_max FROM activity_summary a "
                "JOIN target t ON t.chembl_id = a.target_chembl_id "
                f"WHERE {clause} "
                "ORDER BY a.n_exact + a.n_censored DESC, a.target_chembl_id, a.standard_type, "
                "a.standard_units LIMIT ?",
                (chembl_id, *params, limit),
            ):
                rows.append(
                    ActivityRow(
                        target_chembl_id=row["target_chembl_id"],
                        target_name=row["target_name"],
                        target_type=row["target_type"],
                        organism=row["organism"],
                        standard_type=row["standard_type"],
                        units=row["standard_units"] or None,
                        n_exact_values=row["n_exact"],
                        n_censored_values=row["n_censored"],
                        n_assays=row["n_assays"],
                        median=row["median"],
                        minimum=row["minimum"],
                        maximum=row["maximum"],
                        median_pchembl=row["median_pchembl"],
                        assay_confidence_min=row["assay_confidence_min"],
                        assay_confidence_max=row["assay_confidence_max"],
                    )
                )
    notice = attribution()
    return BioactivitySummary(
        query=echo(identifier),
        matched_by=resolved.matched_by if chembl_id else None,
        match_kind=resolved.match_kind,
        found=chembl_id is not None,
        chembl_id=chembl_id,
        pref_name=pref_name,
        rows=rows,
        rows_matching=int(total),
        truncated=int(total) > len(rows),
        filters=filters,
        note=_note(resolved.note, chembl_id, total),
        source=source_line(notice),
        caveat=CAVEAT,
        attribution=notice,
    )


def _note(resolution_note: str | None, chembl_id: str | None, total: int) -> str | None:
    """Say why there are no rows: no compound, or no summary under these filters."""
    if chembl_id is None:
        return resolution_note
    if total == 0:
        return (
            "no activity summary for this compound under these filters in this subset; that is "
            "not evidence it was never tested"
        )
    return resolution_note
