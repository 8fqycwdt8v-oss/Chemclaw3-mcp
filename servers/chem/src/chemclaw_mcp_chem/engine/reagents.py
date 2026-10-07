"""Resolve the names chemists actually write to the structures every tool demands.

A committed table (`data/records.csv`), not a network call — no server makes one at request time.
Resolution is conservative: an unknown name returns no match, and a name that reads as two
substances is refused (`_AMBIGUOUS_FORMULAS`), because a fabricated structure propagates silently
into calculations and batch records. The indices are built once and fail loudly on an unparsable
SMILES, a spelling claimed twice, or two rows with one structure.
"""

from __future__ import annotations

from difflib import get_close_matches
from functools import lru_cache
from pathlib import Path
from typing import Literal

from mcp_server_kit import Dataset, load_dataset, read_records
from pydantic import BaseModel

from chemclaw_mcp_chem.engine.chem import InvalidSmilesError, require_dative_free_smiles

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

__all__ = [
    "ResolutionSource",
    "ResolvedCompound",
    "UnrecognisedCompound",
    "dataset",
    "density_of",
    "describe_miss",
    "resolve_compound_name",
]


# How an identity was established; a `Literal` because the charge table carries it onto every row
# as provenance.
ResolutionSource = Literal["synonym", "smiles"]


class ResolvedCompound(BaseModel):
    """One resolved identity: the canonical structure plus the name it was recognised as."""

    query: str
    smiles: str
    name: str
    # How the identity was established, so a caller (and the agent) can weigh it: `synonym` is the
    # curated table, `smiles` means the query already was a structure.
    source: ResolutionSource


class UnrecognisedCompound(BaseModel):
    """What a miss says on the wire: that it is one, what was searched, and what would resolve.

    It exists because the miss used to be `None`, and FastMCP writes `None` as *no content at all*:
    the agent received an empty string (audited as `ok`, result `""`) for "aniline", and had to
    infer from silence that the tool had not recognised the name. Silence reads as a broken tool
    as easily as a miss. This says it in words, and says what to pass instead.

    `suggestions` are offered, never substituted: they are the table's own names whose spellings
    are close to what was typed, for a caller who mistyped one ("dipaa"). A name with no near
    neighbour gets none rather than the least-bad match, because "refuse rather than approximate"
    applies to a hint as much as to an answer.
    """

    query: str
    recognised: Literal[False] = False
    # The corpus that was searched, as `name vVERSION`, so the miss is attributable like a hit.
    searched: str
    reason: str
    accepts: str
    suggestions: list[str]


# `difflib` ratio a table spelling must reach to be offered as "did you mean": catches a transposed
# or dropped letter, offers nothing for a name the table does not hold.
_SUGGESTION_CUTOFF = 0.8
_MAX_SUGGESTIONS = 3


# Tokens that are a formula to a chemist and a different substance to RDKit (`CO` parses as
# methanol): `written -> (meaning, its structure, what the parser reads)`. Refused rather than
# resolved, since only the caller knows which reading was meant; the `ValueError` reaches the model
# verbatim.
_AMBIGUOUS_FORMULAS: dict[str, tuple[str, str, str]] = {
    "CO": ("carbon monoxide", "[C-]#[O+]", "methanol"),
    "NO": ("nitric oxide", "[N]=O", "hydroxylamine"),
    "CN": ("cyanide", "[C-]#N", "methylamine"),
    # Every single-element token of the SMILES organic subset: a chemist writes the element and
    # the parser reads its hydride.
    "B": ("boron", "[B]", "borane"),
    "C": ("carbon", "[C]", "methane"),
    "N": ("nitrogen", "N#N", "ammonia"),
    "O": ("oxygen", "O=O", "water"),
    "P": ("phosphorus", "[P]", "phosphine"),
    "S": ("sulfur", "[S]", "hydrogen sulfide"),
    "F": ("fluorine", "FF", "hydrogen fluoride"),
    "Cl": ("chlorine", "ClCl", "hydrogen chloride"),
    "Br": ("bromine", "BrBr", "hydrogen bromide"),
    "I": ("iodine", "II", "hydrogen iodide"),
}


def _normalize(name: str) -> str:
    """Fold a written name to its lookup key: case, whitespace and separator punctuation.

    Non-ASCII is not folded, so the table carries the spelling a keyboard produces.
    """
    folded = name.strip().lower()
    # The curly apostrophe is here on purpose and is not a typo for the straight one: a name
    # pasted out of a document or an ELN carries whichever the editor produced.
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
def _index() -> tuple[dict[str, tuple[str, str]], dict[str, str], dict[str, float]]:
    """Build the three lookups the module answers from, canonicalizing every structure once.

    Cached as one unit, since they are three views of one file.

    Returns:
        Spelling -> (canonical SMILES, display name); canonical SMILES -> display name; and
        canonical SMILES -> density for substances charged by volume.

    Raises:
        ValueError: a row's SMILES does not parse, two rows claim one spelling, or two rows
            canonicalize to one structure.
    """
    table: dict[str, tuple[str, str]] = {}
    by_structure: dict[str, str] = {}
    densities: dict[str, float] = {}
    for row in read_records(dataset()):
        display, raw_smiles = row["name"].strip(), row["smiles"].strip()
        try:
            smiles = require_dative_free_smiles(raw_smiles)
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
        density = row["density_g_per_ml"].strip()
        if density:
            densities[smiles] = float(density)
    return table, by_structure, densities


def resolve_compound_name(name: str) -> ResolvedCompound | None:
    """Resolve a written reagent name (or a SMILES) to a canonical structure, or `None`.

    `None` rather than a guess: a fabricated structure is worse than an honest miss.
    """
    table, by_structure, _ = _index()
    lookup = table.get(_normalize(name))
    if lookup is not None:
        smiles, display = lookup
        return ResolvedCompound(query=name, smiles=smiles, name=display, source="synonym")
    # After the curated table and before the parser, so a reviewed spelling always decides.
    # Case-sensitive, since case tells an element symbol from a name.
    _refuse_an_ambiguous_formula(name)
    # Accept a structure too. The canonicalizer must be strict: a lenient one would resolve every
    # unknown name to itself.
    try:
        canonical = require_dative_free_smiles(name)
    except InvalidSmilesError:
        return None
    return ResolvedCompound(
        query=name,
        smiles=canonical,
        name=by_structure.get(canonical, name),
        source="smiles",
    )


def describe_miss(name: str) -> UnrecognisedCompound:
    """The explicit answer for a name `resolve_compound_name` did not resolve.

    States the limit as well as the miss: there is no name-to-structure service, so the way forward
    for an arbitrary compound is a SMILES.
    """
    table, _, _ = _index()
    manifest = dataset()
    substances = len(set(table.values()))
    suggestions: list[str] = []
    for key in get_close_matches(
        _normalize(name), table, n=_MAX_SUGGESTIONS, cutoff=_SUGGESTION_CUTOFF
    ):
        display = table[key][1]
        if display not in suggestions:
            suggestions.append(display)
    return UnrecognisedCompound(
        query=name,
        searched=f"{manifest.name} v{manifest.version}",
        reason=(
            f"{name.strip()!r} is not a name in this server's reagent table ({substances} "
            "solvents, bases, catalysts, coupling agents and other bench reagents) and does not "
            "parse as a SMILES. This server has no name-to-structure service and makes no outbound "
            "lookup, so a compound name outside that table cannot be resolved here."
        ),
        accepts=(
            "A SMILES string, or one of the table's names or abbreviations (e.g. THF, DIPEA, "
            "K2CO3, Pd(dppf)Cl2). A SMILES you write out from the name yourself is your structure, "
            "not one this table vouched for: tell the chemist so."
        ),
        suggestions=suggestions,
    )


def _refuse_an_ambiguous_formula(name: str) -> None:
    """Refuse a token that reads as one substance to a chemist and another to the SMILES parser.

    Names both readings and both structures, so the caller can say which was meant unambiguously.

    Raises:
        ValueError: `name` is one of the reviewed formula/SMILES collisions.
    """
    reading = _AMBIGUOUS_FORMULAS.get(name.strip())
    if reading is None:
        return
    meant, structure, parsed_as = reading
    raise ValueError(
        f"{name.strip()!r} is ambiguous: a chemist writes it for {meant}, and as a SMILES it is "
        f"{parsed_as}. Pass the structure you mean — {structure} for {meant} — or the name for "
        f"{parsed_as}. Resolving it either way would put a molecular weight nobody chose into a "
        "charge table."
    )


def density_of(name: str) -> float | None:
    """Ambient density in g/mL for a substance that can be charged by volume, or `None`.

    Resolves names, abbreviations and SMILES alike. `None` means unknown or not charged by volume;
    either way the caller must refuse the conversion rather than assume 1 g/mL. Having a density
    does not mean a substance is a solvent in a given experiment.
    """
    match = resolve_compound_name(name)
    return None if match is None else _index()[2].get(match.smiles)
