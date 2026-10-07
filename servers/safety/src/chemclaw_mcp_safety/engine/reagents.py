"""Resolve the abbreviation a chemist writes to the name an ICH table is keyed by.

`ich.py` needs `THF`, `2-MeTHF`, `IPA` and `C1CCOC1` to reach the guideline's spelling without
copying bench synonyms or structures into the transcribed tables. A second copy of `servers/chem`'s
reagent lookup, because servers never import each other; `tests/test_dataset.py` and
`tests/test_fleet_*.py` pin `data/reagents/records.csv` byte-identical to `chem`'s (its unread
density column included). Resolution is conservative: an unknown name returns no match, never a
guess that would attach a real ICH citation to the wrong substance.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from mcp_server_kit import Dataset, load_dataset, read_records
from pydantic import BaseModel

from chemclaw_mcp_safety.engine.chem import InvalidSmilesError, require_canonical_smiles

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "reagents"

__all__ = ["ResolvedCompound", "dataset", "resolve_compound_name"]


class ResolvedCompound(BaseModel):
    """One resolved identity: the canonical structure plus the name it was recognised as."""

    query: str
    smiles: str
    name: str
    # How the identity was established: `synonym` (the curated table) or `smiles` (the query was a
    # structure).
    source: str


def _normalize(name: str) -> str:
    """Fold a written name to its lookup key: case, whitespace and separator punctuation.

    `2-MeTHF`, `2 methf` and `2_MeTHF` share a key; apostrophes fold away, diacritics do not.
    """
    folded = name.strip().lower()
    # Both straight and curly apostrophes: pasted names carry whichever the editor produced.
    for noise in (" ", "-", "_", "'", "’"):  # noqa: RUF001
        folded = folded.replace(noise, "")
    return folded


@lru_cache(maxsize=1)
def dataset() -> Dataset:
    """The vendored reagent table's manifest, verified against its checksum on first use."""
    return load_dataset(DATA_DIR)


def _split(raw: str) -> list[str]:
    """Split a semicolon-delimited cell, dropping blanks."""
    return [part.strip() for part in raw.split(";") if part.strip()]


@lru_cache(maxsize=1)
def _index() -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    """Build the two lookups this module answers from, canonicalising every structure once.

    Cached together because they are two views of one file.

    Returns:
        The spelling -> (canonical SMILES, display name) table, and the canonical SMILES -> display
            name map.

    Raises:
        ValueError: A row's SMILES does not parse, two rows claim one spelling, or two rows
            canonicalise to one structure.
    """
    table: dict[str, tuple[str, str]] = {}
    by_structure: dict[str, str] = {}
    for row in read_records(dataset()):
        display, raw_smiles = row["name"].strip(), row["smiles"].strip()
        try:
            smiles = require_canonical_smiles(raw_smiles)
        except InvalidSmilesError as exc:
            raise ValueError(
                f"reagent table entry {display!r} has unparseable SMILES: {exc}"
            ) from exc
        if smiles in by_structure:
            raise ValueError(
                f"the reagent table gives {smiles} two names, {by_structure[smiles]!r} and "
                f"{display!r}; a structure that resolves two ways resolves neither"
            )
        by_structure[smiles] = display
        for spelling in _split(row["synonyms"]):
            key = _normalize(spelling)
            if key in table:
                raise ValueError(
                    f"the reagent table maps {spelling!r} to both {table[key][1]!r} and "
                    f"{display!r}; a name that resolves two ways resolves neither"
                )
            table[key] = (smiles, display)
    return table, by_structure


def resolve_compound_name(name: str) -> ResolvedCompound | None:
    """Resolve a written reagent name (or a SMILES) to a canonical structure, or `None`.

    Never guesses: the caller is an ICH limit lookup, where a wrong structure means a wrong cited
    limit.
    """
    table, by_structure = _index()
    lookup = table.get(_normalize(name))
    if lookup is not None:
        smiles, display = lookup
        return ResolvedCompound(query=name, smiles=smiles, name=display, source="synonym")
    # Accept a structure too. The strict canonicaliser is essential: a lenient one would resolve
    # every unknown name to itself as a fabricated structure.
    try:
        canonical = require_canonical_smiles(name)
    except InvalidSmilesError:
        return None
    return ResolvedCompound(
        query=name,
        smiles=canonical,
        name=by_structure.get(canonical, name),
        source="smiles",
    )
