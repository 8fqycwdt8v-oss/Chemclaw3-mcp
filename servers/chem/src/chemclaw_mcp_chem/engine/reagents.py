"""Resolve the names chemists actually write to the structures every tool demands.

Every chemistry capability in this fleet speaks SMILES — `solvent_properties(name)` is the
exception, not the rule, and Chemclaw3's own calculators all take structures. Chemists write
`Pd(dppf)Cl2`, `DIPEA`, `2-MeTHF`, `TBTU`, and ELN free text writes the same. This is the bridge:
`resolve_compound` is it made a tool, and the charge table calls it once per charged species.

**Deliberately a committed table, not a network call** — which in this repository is not a choice
but the rule (`CLAUDE.md`, "No egress. Ever."). It is also the right design on its own terms: the
reagents a process-chemistry group uses daily are a small, stable, high-value set, and a table is
deterministic, reviewable in a pull request, and citable.

Resolution is **conservative**: an unknown name returns no match rather than a guess, and a name
that reads as two different substances is refused outright rather than resolved to one of them (see
`_AMBIGUOUS_FORMULAS`). Fabricating a structure from a name is the one failure worse than the gap —
a wrong structure propagates silently into a calculation, a search, and eventually a chemist's batch
record.

The corpus is `data/records.csv`, one row per substance, ported from Chemclaw3's
`chemclaw.core.reagents`. Its three indices are built once on first use and fail loudly rather than
dropping a row: a SMILES that does not parse, a spelling claimed by two substances, and two
substances that canonicalize to one structure are all errors there, because all three are silent
at call time.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from mcp_server_kit import Dataset, load_dataset, read_records
from pydantic import BaseModel

from chemclaw_mcp_chem.engine.chem import InvalidSmilesError, require_canonical_smiles

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

__all__ = [
    "ResolutionSource",
    "ResolvedCompound",
    "dataset",
    "density_of",
    "resolve_compound_name",
]


# How an identity was established. A `Literal` rather than a bare `str` because the charge table
# carries it onto every row as provenance, and a third value nobody declared would arrive there as
# an attribution a reader cannot check.
ResolutionSource = Literal["synonym", "smiles"]


class ResolvedCompound(BaseModel):
    """One resolved identity: the canonical structure plus the name it was recognised as."""

    query: str
    smiles: str
    name: str
    # How the identity was established, so a caller (and the agent) can weigh it: `synonym` is the
    # curated table, `smiles` means the query already was a structure.
    source: ResolutionSource


# **The tokens that are a formula to a chemist and a different substance to RDKit.** Each row is
# `written -> (what a chemist means, its structure, what the SMILES parser reads instead)`, and each
# was checked against the installed RDKit rather than reasoned about: these are exactly the
# formula-shaped strings that parse. `CO2`, `SO2`, `H2O` and `NH3` are not here because they do not
# parse at all, which is why the gap looked honest — only the holes that happen to parse fail
# silently.
#
# The harm is not hypothetical and it is not small. `stoichiometry_table` with `reagents=["CO"]`
# returned a complete charge table with an empty `unresolved`, naming methanol at MW 32.042 and
# instructing 30.61 g to be weighed out for a gas whose MW is 28.010 — and `green_metrics` built
# from that `mass_g` column inherited it.
#
# So this is a refusal rather than a resolution, per the fleet's "refuse rather than approximate":
# only the caller knows which reading was meant, and a `ValueError` reaches the model verbatim.
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

    `2-MeTHF`, `2 methf` and `2_MeTHF` are one key; `Hünig's base` and `hunigsbase` are not, because
    the apostrophe folds away and the umlaut does not — the table therefore carries the spelling a
    keyboard produces. Everything dropped here is punctuation a chemist varies without meaning to.
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

    Returned together and cached as one unit because they are three views of one file, and a
    partially rebuilt set of them would be a table that disagreed with itself.

    Returns:
        The spelling -> (canonical SMILES, display name) table; the reverse canonical SMILES ->
        display name map, which is what lets a caller who typed a structure get a name back; and
        the canonical SMILES -> density map for the substances that can be charged by volume.

    Raises:
        ValueError: a row's SMILES does not parse, two rows claim one spelling, or two rows
            canonicalize to one structure. All three are silent at call time and loud here.
    """
    table: dict[str, tuple[str, str]] = {}
    by_structure: dict[str, str] = {}
    densities: dict[str, float] = {}
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
        density = row["density_g_per_ml"].strip()
        if density:
            densities[smiles] = float(density)
    return table, by_structure, densities


def resolve_compound_name(name: str) -> ResolvedCompound | None:
    """Resolve a written reagent name (or a SMILES) to a canonical structure, or `None`.

    Returns `None` rather than guessing: a fabricated structure propagates silently into a
    calculation, a similarity search, and eventually a chemist's batch record, which is strictly
    worse than an honest miss.
    """
    table, by_structure, _ = _index()
    lookup = table.get(_normalize(name))
    if lookup is not None:
        smiles, display = lookup
        return ResolvedCompound(query=name, smiles=smiles, name=display, source="synonym")
    # After the curated table and before the parser, so a reviewed spelling always decides and
    # only the strings nobody has ruled on are refused. Case-sensitive on the query as typed,
    # because that is what tells an element symbol from a name.
    _refuse_an_ambiguous_formula(name)
    # A caller may already hold a structure; accepting it here means one entry point for "give me
    # the canonical form of whatever the chemist typed". The strict canonicalizer is essential: a
    # lenient one returns its input unparsed, which would resolve every unknown name to itself as
    # a fabricated structure — exactly the failure this module exists to prevent.
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


def _refuse_an_ambiguous_formula(name: str) -> None:
    """Refuse a token that reads as one substance to a chemist and another to the SMILES parser.

    Both readings are named, and so is the structure of the one the parser will not give you, so
    the caller can say which was meant in a form that cannot be misread. The alternative is what
    this used to do: resolve it to the parser's reading, with a confident display name and
    `source="smiles"`, and let a mass reach a batch record.

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

    Takes whatever the chemist wrote (a name, an abbreviation, or a SMILES) and resolves it the
    same way every other entry point does, so `THF`, `tetrahydrofuran` and `C1CCOC1` agree.

    `None` is the load-bearing answer, and it means two things a caller must keep apart: the name
    is unknown, or it is a known substance that is not charged by volume. Either way the caller
    must refuse to convert a volume into a mass rather than assume 1 g/mL — a guessed density is a
    weighing error that looks like an answer, and for the principal solvent it silently rewrites
    every mass metric derived from it.

    Note what this does **not** say. Having a density is a fact about a substance; being charged by
    volume is a fact about one experiment. Acetic acid at 1.5 equiv, water in a hydrolysis, DMSO as
    the Swern oxidant and DMF as the Vilsmeier reagent all have a density on file and are all
    routinely charged by molar equivalent, so this must never be read as "is it a solvent?".
    """
    match = resolve_compound_name(name)
    return None if match is None else _index()[2].get(match.smiles)
