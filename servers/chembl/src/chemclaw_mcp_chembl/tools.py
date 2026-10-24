"""The `chembl` MCP tool surface: one compound lookup and one bioactivity summary.

The tool docstrings are the prompt. Two tools because the questions differ: "which ChEMBL compound
is this" (identity) and "what has been measured for it" (aggregated data). Both are read-only
queries over one vendored SQLite subset and both return the CC BY-SA 3.0 attribution as a
structured field. The queries are small and indexed but touch a file, so each runs in a worker
thread.
"""

from __future__ import annotations

import asyncio

from mcp.server.fastmcp import FastMCP

from chemclaw_mcp_chembl.engine.bioactivity import (
    DEFAULT_ROWS,
    BioactivitySummary,
    bioactivity_summary,
)
from chemclaw_mcp_chembl.engine.lookup import Kind, LookupResult, lookup

server = FastMCP("chembl")


@server.tool()
async def chembl_lookup_compound(identifier: str, kind: Kind = "auto") -> LookupResult:
    """Find a compound in the ChEMBL subset by ChEMBL id, name or structure.

    Use this to ask "is this compound in ChEMBL, and under which id" — before quoting a ChEMBL id,
    or to turn a drug name or a structure into the id `chembl_bioactivity_summary` needs.

    `kind` says what `identifier` is: `chembl_id` (`CHEMBL25`), `name` (a preferred name or
    recorded synonym, whole — `aspirin`, `Gleevec`), or `smiles`. `auto` tries id, then name, then
    SMILES, and `matched_by` reports which one answered. Matching is exact and never fuzzy: a name
    that is not recorded is a miss, not the nearest spelling.

    A structure is matched on its InChIKey. `match_kind` says how: `exact` (the same key),
    `same_connectivity` (the same skeleton: a flat or differently-stereo SMILES finds every
    stereoisomer ChEMBL holds, and a charged form finds its neutral one — they are different
    compounds, choose by id), or the same two `largest_fragment_*` when you pass a salt or solvate
    and only its parent form is held. A salt record names its `parent_chembl_id`.

    **This is a subset of ChEMBL, not ChEMBL.** A miss means this subset does not hold the
    compound (it keeps a selection, not all of ChEMBL's millions), never that the compound is
    unknown or has no data. Say exactly that. `max_phase` is ChEMBL's record of the highest trial
    phase reached; it is not a claim of efficacy, safety or approval status in any jurisdiction,
    and this tool makes no clinical statement.

    **A SMILES this cannot read in full is refused, not searched**: one structure, no name beside
    it, no whitespace. A string over the fleet's length bound is refused too.

    Every answer carries `attribution` — ChEMBL release, CC BY-SA 3.0, source URL and the changes
    made (a subset). Keep the ChEMBL id and release whenever you quote it.

    Args:
        identifier: A ChEMBL id, a compound name or a SMILES.
        kind: `auto` (default), `chembl_id`, `name` or `smiles`.

    Returns:
        `compounds` (ChEMBL id, names, synonyms, formula, molecular weight in g/mol, ChEMBL's
        canonical SMILES, InChIKey, parent, and how many aggregated activity rows exist), at most
        25, with `truncated` when more matched; `found`, `matched_by`, `match_kind`, a `note` on a
        miss or a loose match, `source`, `caveat` and `attribution`.
    """
    return await asyncio.to_thread(lookup, identifier, kind)


@server.tool()
async def chembl_bioactivity_summary(
    identifier: str,
    kind: Kind = "auto",
    standard_type: str | None = None,
    target_chembl_id: str | None = None,
    min_confidence_score: int | None = None,
    limit: int = DEFAULT_ROWS,
) -> BioactivitySummary:
    """Summarise what ChEMBL records as measured for one compound, per target and standard type.

    Use this for "what is known about this compound's activity" — which targets it was measured
    against, with which assay type (IC50, Ki, Kd, EC50, ...), how many times, and the spread. Find
    the compound first with `chembl_lookup_compound` when you have only a name or a structure;
    an identifier that matches several compounds is refused and the candidates named, so pass a
    ChEMBL id.

    Each row aggregates ChEMBL activities with an exact value over one compound, one target, one
    standard type and one unit: `n_exact_values`, `median`, `minimum` and `maximum` **in the row's
    `units`** (ChEMBL's standard unit, usually nM for potencies; never converted or merged across
    units), `n_censored_values` (reported only as '>' or '<', counted and never averaged),
    `n_assays`, `median_pchembl` where ChEMBL computed one (unitless, higher is more potent), and
    the lowest and highest ChEMBL **assay confidence score** (0-9; 9 means a direct single-protein
    target assignment, lower scores mean the target assignment is weaker or ambiguous). Filter with
    `standard_type`, `target_chembl_id` and `min_confidence_score`; rows come most-measured first.

    **A summary is not a record and not a recommendation.** Medians pool different assays,
    species, conditions and laboratories; two nM figures from different assays are not comparable
    without reading the assays in ChEMBL. A median is not a measured potency of any one
    experiment. Nothing here is a clinical, safety, efficacy or dosing statement, and it must not
    be reported as one. No rows means this subset holds no summary under these filters — not that
    the compound is inactive or was never tested.

    Every answer carries `attribution` — ChEMBL release, CC BY-SA 3.0, source URL and the changes
    made (subset and aggregation). Quote the ChEMBL id and release with any value.

    Args:
        identifier: A ChEMBL id, a compound name or a SMILES (see `chembl_lookup_compound`).
        kind: `auto` (default), `chembl_id`, `name` or `smiles`.
        standard_type: Keep only this ChEMBL standard type, e.g. `IC50`, `Ki`, `Kd`, `EC50`.
        target_chembl_id: Keep only this target, e.g. `CHEMBL203`.
        min_confidence_score: Keep rows whose highest assay confidence score is at least this
            (0-9); `9` keeps direct single-protein assignments.
        limit: Rows to return, 1-200 (default 50).

    Returns:
        `rows` (target id, name, type and organism; standard type; `units`; counts; `median`,
        `minimum`, `maximum`; `median_pchembl`; confidence range), `rows_matching` and `truncated`,
        the `filters` applied, a `note`, `source`, `caveat` and `attribution`.
    """
    return await asyncio.to_thread(
        bioactivity_summary,
        identifier,
        kind,
        standard_type,
        target_chembl_id,
        min_confidence_score,
        limit,
    )
