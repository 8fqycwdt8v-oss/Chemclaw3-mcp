"""A compound found by ChEMBL id, name or structure — exactly, never by the nearest candidate.

An id is matched case-insensitively; a name is matched whole, after case and whitespace folding,
against ChEMBL's preferred name and its recorded synonyms; a structure is matched on its InChIKey,
full first and then its connectivity block, so a flat SMILES finds the stereoisomers ChEMBL holds
and the answer says so. Nothing is fuzzy: a miss is `found: false`, not a similar molecule.

Invariants: every `LookupResult` carries the attribution and the caveat; a structure this server
cannot read in full is refused (`chem.InvalidSmilesError`), never searched as a smaller molecule.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Literal, get_args

from mcp_server_kit.limits import echo, smiles_length_error
from pydantic import BaseModel, Field
from rdkit import Chem
from rdkit.Chem.MolStandardize import rdMolStandardize

from chemclaw_mcp_chembl.engine import store
from chemclaw_mcp_chembl.engine.attribution import CAVEAT, Attribution, attribution, source_line
from chemclaw_mcp_chembl.engine.chem import InvalidSmilesError, inchi_key, require_molecule

__all__ = [
    "MAX_COMPOUNDS",
    "Compound",
    "Kind",
    "LookupResult",
    "MatchKind",
    "Resolved",
    "lookup",
    "resolve",
]

#: Compounds one lookup returns; a name or a flat structure can match many salts and isomers.
MAX_COMPOUNDS = 25
#: Synonyms listed per compound (ChEMBL records dozens of trade names for a drug).
MAX_SYNONYMS = 25

Kind = Literal["auto", "chembl_id", "name", "smiles"]
MatchedBy = Literal["chembl_id", "name", "smiles"]
MatchKind = Literal[
    "exact",
    "same_connectivity",
    "largest_fragment_exact",
    "largest_fragment_same_connectivity",
]

_CHEMBL_ID = re.compile(r"CHEMBL[0-9]+", re.IGNORECASE)
_SKELETON_CHARS = 14

_COLUMNS = (
    "chembl_id, pref_name, max_phase, molecule_type, formula, mol_weight, canonical_smiles, "
    "inchi_key, parent_chembl_id"
)


class Compound(BaseModel):
    """One ChEMBL compound as the subset records it."""

    chembl_id: str = Field(description="ChEMBL molecule id, e.g. CHEMBL25. Keep it when quoting.")
    pref_name: str | None = Field(description="ChEMBL's preferred name; null for most compounds.")
    synonyms: list[str] = Field(description="Recorded synonyms (INN, trade names, codes).")
    synonyms_truncated: bool = Field(description="True when more synonyms exist than are listed.")
    molecule_type: str | None = Field(description="e.g. 'Small molecule', 'Antibody', 'Protein'.")
    max_phase: float | None = Field(
        description="ChEMBL's annotation of the highest clinical-trial phase reached (4 = "
        "approved somewhere; 0.5 = early phase 1; null = none recorded). A record of a trial "
        "stage, not an endorsement of efficacy or safety."
    )
    molecular_formula: str | None
    molecular_weight: float | None = Field(description="Of the recorded form, in g/mol.")
    canonical_smiles: str | None = Field(description="ChEMBL's own canonical SMILES.")
    standard_inchi_key: str | None
    parent_chembl_id: str | None = Field(
        description="The parent compound when this record is a salt or solvate form."
    )
    activity_summaries: int = Field(
        description="How many aggregated activity rows `chembl_bioactivity_summary` holds for it."
    )


class LookupResult(BaseModel):
    """What a lookup found, how it matched, and the notice that goes with every answer."""

    query: str = Field(description="The caller's identifier, echoed (bounded).")
    matched_by: MatchedBy | None = Field(
        description="Which identifier kind matched; null on a miss."
    )
    match_kind: MatchKind | None = Field(
        description="For a structure: 'exact' (same InChIKey), 'same_connectivity' (same "
        "skeleton; stereochemistry, isotopes or protonation state may differ), or the same two "
        "for the input's largest fragment (a salt or solvate written with its counter-ion). Null "
        "for an id or a name."
    )
    found: bool
    compounds: list[Compound]
    truncated: bool = Field(description="True when more compounds matched than are returned.")
    note: str | None = Field(description="Why a miss missed, or what a loose match means.")
    source: str
    caveat: str
    attribution: Attribution


@dataclass(frozen=True, slots=True)
class Resolved:
    """The internal result of resolving an identifier, before it is shaped for a tool."""

    matched_by: MatchedBy | None
    match_kind: MatchKind | None
    ids: tuple[str, ...]
    truncated: bool
    note: str | None


def _fold(name: str) -> str:
    """The comparison form of a name: casefolded, whitespace collapsed."""
    return " ".join(name.casefold().split())


def _check_text(query: str) -> str:
    """The stripped identifier, refused if blank or longer than the fleet's string bound."""
    stripped = query.strip()
    if not stripped:
        raise ValueError("the identifier is empty; give a ChEMBL id, a compound name or a SMILES")
    if reason := smiles_length_error(stripped, subject="this identifier"):
        raise ValueError(reason)
    return stripped


def _by_id(connection: sqlite3.Connection, text: str) -> Resolved:
    row = connection.execute(
        "SELECT chembl_id FROM compound WHERE chembl_id = ?", (text.upper(),)
    ).fetchone()
    note = None if row else "no compound with that id is in this subset"
    return Resolved("chembl_id", None, (row["chembl_id"],) if row else (), False, note)


def _by_name(connection: sqlite3.Connection, text: str) -> Resolved:
    rows = connection.execute(
        "SELECT DISTINCT chembl_id FROM name WHERE name_norm = ? ORDER BY chembl_id LIMIT ?",
        (_fold(text), MAX_COMPOUNDS + 1),
    ).fetchall()
    ids = tuple(str(row["chembl_id"]) for row in rows[:MAX_COMPOUNDS])
    note = None if ids else "no compound in this subset has that name or synonym"
    return Resolved("name", None, ids, len(rows) > MAX_COMPOUNDS, note)


def _matches(connection: sqlite3.Connection, key: str) -> tuple[str, list[str]]:
    """The (layer, ids) matching `key`: the full key if any compound has it, else its skeleton."""
    exact = connection.execute(
        "SELECT chembl_id FROM compound WHERE inchi_key = ? ORDER BY chembl_id LIMIT ?",
        (key, MAX_COMPOUNDS + 1),
    ).fetchall()
    if exact:
        return "exact", [str(row["chembl_id"]) for row in exact]
    loose = connection.execute(
        "SELECT chembl_id FROM compound WHERE skeleton = ? ORDER BY chembl_id LIMIT ?",
        (key[:_SKELETON_CHARS], MAX_COMPOUNDS + 1),
    ).fetchall()
    return "same_connectivity", [str(row["chembl_id"]) for row in loose]


_LAYER_NOTE = {
    "same_connectivity": "no compound has this exact InChIKey; these share its skeleton (same "
    "atoms and connectivity; stereochemistry, isotopes or protonation state may differ). They are "
    "different compounds: choose by ChEMBL id.",
    "largest_fragment_exact": "the input has several fragments; these match its largest fragment "
    "(its parent form), not the salt or solvate as written.",
    "largest_fragment_same_connectivity": "the input has several fragments; these share the "
    "skeleton of its largest fragment (stereochemistry, isotopes or protonation may differ).",
}


def _by_smiles(connection: sqlite3.Connection, text: str) -> Resolved:
    mol = require_molecule(text)
    layer, ids = _matches(connection, inchi_key(mol))
    kind: MatchKind = "exact" if layer == "exact" else "same_connectivity"
    if not ids and len(Chem.GetMolFrags(mol)) > 1:
        largest = rdMolStandardize.LargestFragmentChooser().choose(mol)
        layer, ids = _matches(connection, inchi_key(largest))
        kind = (
            "largest_fragment_exact" if layer == "exact" else "largest_fragment_same_connectivity"
        )
    if not ids:
        return Resolved("smiles", None, (), False, "no compound in this subset has this structure")
    return Resolved(
        "smiles", kind, tuple(ids[:MAX_COMPOUNDS]), len(ids) > MAX_COMPOUNDS, _LAYER_NOTE.get(kind)
    )


def resolve(connection: sqlite3.Connection, query: str, kind: Kind) -> Resolved:
    """The ids `query` identifies under `kind`; `auto` tries id, then name, then structure.

    Raises:
        ValueError: The identifier is blank or too long, or `kind='smiles'` was asked of a string
            RDKit cannot read whole (`InvalidSmilesError`).
    """
    if kind not in get_args(Kind):
        raise ValueError(f"kind must be one of {', '.join(get_args(Kind))}")
    text = _check_text(query)
    if kind == "chembl_id" or (kind == "auto" and _CHEMBL_ID.fullmatch(text)):
        return _by_id(connection, text)
    if kind == "name":
        return _by_name(connection, text)
    if kind == "smiles":
        return _by_smiles(connection, text)
    named = _by_name(connection, text)
    if named.ids:
        return named
    try:
        return _by_smiles(connection, text)
    except InvalidSmilesError:
        return Resolved(
            None,
            None,
            (),
            False,
            "not a ChEMBL id, a name in this subset, or a SMILES RDKit can read in full",
        )


def _compound(connection: sqlite3.Connection, chembl_id: str) -> Compound:
    row = connection.execute(
        f"SELECT {_COLUMNS} FROM compound WHERE chembl_id = ?",  # noqa: S608 - fixed column list
        (chembl_id,),
    ).fetchone()
    synonyms = [
        str(r["name"])
        for r in connection.execute(
            "SELECT name FROM name WHERE chembl_id = ? AND is_pref = 0 "
            "ORDER BY length(name), name LIMIT ?",
            (chembl_id, MAX_SYNONYMS + 1),
        )
    ]
    n_rows = connection.execute(
        "SELECT COUNT(*) FROM activity_summary WHERE compound_chembl_id = ?", (chembl_id,)
    ).fetchone()[0]
    return Compound(
        chembl_id=row["chembl_id"],
        pref_name=row["pref_name"],
        synonyms=synonyms[:MAX_SYNONYMS],
        synonyms_truncated=len(synonyms) > MAX_SYNONYMS,
        molecule_type=row["molecule_type"],
        max_phase=row["max_phase"],
        molecular_formula=row["formula"],
        molecular_weight=row["mol_weight"],
        canonical_smiles=row["canonical_smiles"],
        standard_inchi_key=row["inchi_key"],
        parent_chembl_id=row["parent_chembl_id"],
        activity_summaries=int(n_rows),
    )


def lookup(query: str, kind: Kind = "auto") -> LookupResult:
    """Find compounds by id, name or structure; see the module docstring for what matches."""
    with store.connect() as connection:
        resolved = resolve(connection, query, kind)
        compounds = [_compound(connection, chembl_id) for chembl_id in resolved.ids]
    notice = attribution()
    return LookupResult(
        query=echo(query),
        matched_by=resolved.matched_by if compounds else None,
        match_kind=resolved.match_kind,
        found=bool(compounds),
        compounds=compounds,
        truncated=resolved.truncated,
        note=resolved.note,
        source=source_line(notice),
        caveat=CAVEAT,
        attribution=notice,
    )
