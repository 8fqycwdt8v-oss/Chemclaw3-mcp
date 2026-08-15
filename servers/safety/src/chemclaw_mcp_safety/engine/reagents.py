"""Resolve the abbreviation a chemist writes to the name an ICH table is keyed by.

`ich.py` needs one thing from this module: `THF`, `2-MeTHF`, `IPA` and `C1CCOC1` must all reach the
guideline's own spelling, without every one of those spellings having to be copied into the
transcribed tables. The ICH files carry the synonyms a *regulatory* reader writes; this carries the
ones a bench chemist writes, plus the structures — and a structure is the case the guideline files
cannot cover at all, because a SMILES is not a spelling of a name.

**This is a second copy of `servers/chem/src/chemclaw_mcp_chem/engine/reagents.py`'s lookup, over a
byte-identical corpus.** That is deliberate and it is what the repository's central rule costs: one
server never imports another, so a capability two servers both need is carried by both. The
duplication is made safe rather than merely accepted — `tests/test_dataset.py` pins this copy of
`data/reagents/records.csv` byte-identical to `chem`'s, and `tests/test_fleet.py` asserts it across
the two servers, so the two cannot answer differently about one substance.

**What was deliberately left behind.** `chem`'s copy also carries `density_of` and the density index
behind it, because its charge table turns process volumes into masses. Nothing here asks that
question, so neither is present; the CSV's `density_g_per_ml` column is carried unread so the file
stays byte-identical to the one `chem` ships.

Resolution is **conservative**: an unknown name returns no match rather than a guess. That property
is the whole reason the ICH lookup may use this at all — a fabricated structure would turn a miss
("this system does not carry the number") into a confident wrong row with a real ICH citation
attached to it, which is worse than the fabrication the tables were transcribed to end.
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
    # How the identity was established: `synonym` is the curated table, `smiles` means the query
    # already was a structure.
    source: str


def _normalize(name: str) -> str:
    """Fold a written name to its lookup key: case, whitespace and separator punctuation.

    `2-MeTHF`, `2 methf` and `2_MeTHF` are one key; `Hünig's base` and `hunigsbase` are not, because
    the apostrophe folds away and the umlaut does not — the table therefore carries the spelling a
    keyboard produces. Everything dropped here is punctuation a chemist varies without meaning to.
    """
    folded = name.strip().lower()
    # The curly apostrophe is here on purpose and is not a typo for the straight one: a name pasted
    # out of a document or an ELN carries whichever the editor produced.
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
    """Build the two lookups this module answers from, canonicalizing every structure once.

    Returned together and cached as one unit because they are two views of one file, and a partially
    rebuilt pair would be a table that disagreed with itself.

    Returns:
        The spelling -> (canonical SMILES, display name) table, and the reverse canonical SMILES ->
        display name map, which is what lets a caller who typed a structure get a name back.

    Raises:
        ValueError: a row's SMILES does not parse, two rows claim one spelling, or two rows
            canonicalize to one structure. All three are silent at call time and loud here.
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

    Returns `None` rather than guessing: the one caller is an ICH limit lookup, and a fabricated
    structure there would hand a chemist somebody else's permitted daily exposure under a genuine
    guideline citation.
    """
    table, by_structure = _index()
    lookup = table.get(_normalize(name))
    if lookup is not None:
        smiles, display = lookup
        return ResolvedCompound(query=name, smiles=smiles, name=display, source="synonym")
    # A caller may already hold a structure; accepting it here means one entry point for "give me
    # the canonical form of whatever the chemist typed". The strict canonicalizer is essential: a
    # lenient one returns its input unparsed, which would resolve every unknown name to itself as a
    # fabricated structure — exactly the failure this module exists to prevent.
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
