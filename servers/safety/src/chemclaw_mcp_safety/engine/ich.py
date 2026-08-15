"""ICH Q3C / Q3D limit lookup: the number comes from a vendored table or not at all.

Why this exists. In a Chemclaw3 live run a chemist asked for the palladium limit and the system
recited a PDE from training as though it were the record. The value was correct, which makes it
worse rather than better: a correct recalled limit trains a reader to trust the next one, and there
is nothing behind either.

What this is. Two transcribed reference tables (`data/ich_q3c/`, `data/ich_q3d/`) and one lookup
over both. Q3C residual-solvent classes and limits, Q3D elemental-impurity permitted daily
exposures. Every row carries the guideline, its revision, and the table it came from, so a reader
can open the source document at the right page and check the figure. A substance the tables do not
carry returns a **miss** that says so — never a nearby value, never a recalled one.

What this is emphatically *not*: a risk assessment. Deciding whether a given process needs a given
control, what specification an intermediate should carry, or how a PDE converts into a limit on an
API is judgement about a process. This module's job is to supply the number that judgement needs.

**Why these two tables are checksummed rather than configurable.** Nobody has their own Q3C: the
values are fixed by a published guideline, and a deployment quietly substituting a different PDE
table is the failure mode rather than a feature. Chemclaw3 resolved them against `__file__` for that
reason; here they are vendored corpora with a `dataset.json` each, loaded through the same
`read_table` the two SMARTS tables use — so a truncated or swapped file is caught by its checksum
and reported as a named table fault, rather than becoming a *shorter guideline* that answers "this
system does not carry the number" about a substance it does.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, computed_field

from chemclaw_mcp_safety.engine.reagents import resolve_compound_name
from chemclaw_mcp_safety.engine.screen import read_table

__all__ = [
    "Q3C_DIR",
    "Q3C_FILE",
    "Q3D_DIR",
    "Q3D_FILE",
    "ImpurityLimit",
    "ImpurityLimitLookup",
    "LimitValue",
    "Q3cTable",
    "Q3dTable",
    "impurity_limit",
    "index",
]

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
Q3C_DIR = _DATA_DIR / "ich_q3c"
Q3C_FILE = "ich_q3c.yaml"
Q3D_DIR = _DATA_DIR / "ich_q3d"
Q3D_FILE = "ich_q3d.yaml"


class LimitValue(BaseModel):
    """One number from a guideline table, with the basis it is quoted on and its unit.

    A list of these rather than named fields per guideline, because Q3C quotes a concentration in
    ppm and a PDE in mg/day while Q3D quotes three route-specific PDEs in µg/day. One shape lets the
    agent read either answer without knowing in advance which guideline covers the substance — which
    is exactly the knowledge it was missing when it invented the numbers instead.
    """

    basis: str = Field(min_length=1)
    value: float
    unit: str = Field(min_length=1)


class ImpurityLimit(BaseModel):
    """One transcribed row: what the guideline calls it, what class it is in, and its limits."""

    substance: str
    guideline: str
    limit_class: str
    class_meaning: str
    limits: list[LimitValue]
    citation: str


class ImpurityLimitLookup(BaseModel):
    """The result of one lookup — a row, or an explicit miss.

    The miss is the load-bearing half, and `verdict` is why this is a model rather than an
    `ImpurityLimit | None`. `screen.py` learned the lesson the expensive way: a caveat that lives
    only in a tool docstring is read once when the tool is defined, while the *payload* is what sits
    in the context window as the answer is written. So the distinction between "this system has no
    row for that" and "no limit exists" is carried in the result itself.
    """

    query: str
    limit: ImpurityLimit | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> str:
        """A one-line summary for a human — and, on a miss, the sentence that prevents a guess."""
        if self.limit is None:
            return (
                f"No entry for {self.query!r} in the transcribed ICH Q3C/Q3D tables. That means "
                "this system does not carry the number — not that no limit exists. Read it from "
                "the guideline; do not state one from memory."
            )
        return (
            f"{self.limit.substance}: {self.limit.citation}. Quote the citation with the number, "
            "and note that a limit is not a risk assessment."
        )


class _Q3cSolvent(BaseModel):
    """One row of ICH Q3C Table 1, 2 or 3 as transcribed."""

    name: str = Field(min_length=1)
    synonyms: list[str] = Field(default_factory=list)
    solvent_class: Literal["1", "2", "3"]
    concentration_limit_ppm: float
    # Class 1 is quoted as a concentration limit only; Classes 2 and 3 also carry a PDE.
    pde_mg_per_day: float | None = None
    concern: str | None = None


class _ClassNote(BaseModel):
    """Which table a class lives in, what membership means, and how its numbers are quoted.

    `pde_basis`/`concentration_basis` exist because Class 3's two numbers are **not** what Classes 1
    and 2's are. Q3C assigns those a solvent-specific PDE; Class 3 has no per-solvent value at all,
    only a general statement that 50 mg/day *or more* is acceptable without justification. Both were
    rendered under the same bare `basis="PDE"`, so the machine-readable half — which is what an
    answer quotes — asserted a solvent-specific limit that the guideline does not contain, and did
    it under a real citation. That is the exact shape of failure this table was built to end, so the
    distinction lives on the number rather than only in the prose `meaning` beside it.
    """

    table: str = Field(min_length=1)
    meaning: str = Field(min_length=1)
    pde_basis: str = Field(default="PDE", min_length=1)
    concentration_basis: str = Field(default="concentration limit", min_length=1)


class Q3cTable(BaseModel):
    """The parsed residual-solvent file.

    Public, like `Q3dTable`, so `tests/test_dataset.py` can check the corpus against itself without
    reaching for a private name — the ppm/PDE identity is a property of the transcription, and the
    test that catches a transposed digit has to be able to state it.
    """

    guideline: str = Field(min_length=1)
    classes: dict[str, _ClassNote]
    solvents: list[_Q3cSolvent] = Field(min_length=1)


class _Q3dElement(BaseModel):
    """One row of ICH Q3D Table A.2.1 as transcribed."""

    symbol: str = Field(min_length=1)
    name: str = Field(min_length=1)
    synonyms: list[str] = Field(default_factory=list)
    element_class: Literal["1", "2A", "2B", "3"]
    oral_pde_ug_per_day: float
    parenteral_pde_ug_per_day: float
    inhalation_pde_ug_per_day: float


class Q3dTable(BaseModel):
    """The parsed elemental-impurity file."""

    guideline: str = Field(min_length=1)
    table: str = Field(min_length=1)
    classes: dict[str, str]
    elements: list[_Q3dElement] = Field(min_length=1)


def _fold(text: str) -> str:
    """Fold a written name to its lookup key: case, whitespace and separator punctuation.

    Deliberately the same *idea* as `reagents.py`'s fold but not the same function: this one also
    drops commas and periods, so `N,N-Dimethylformamide` and `NN dimethylformamide` land on one key.
    Both the table's spellings and the query go through it, so the two agree by construction.
    """
    return "".join(character for character in text.lower() if character.isalnum())


def _register(index: dict[str, ImpurityLimit], keys: list[str], limit: ImpurityLimit) -> None:
    """Index one row under every spelling it answers to, refusing a collision.

    A collision means two rows claim one name, and whichever loaded second would silently win — the
    reader would get a limit for a different substance with a real citation attached to it. That is
    the one failure worse than a miss, so it stops the load instead.
    """
    for key in keys:
        folded = _fold(key)
        existing = index.get(folded)
        if existing is not None and existing.substance != limit.substance:
            raise ValueError(
                f"ICH table entries {existing.substance!r} and {limit.substance!r} "
                f"both answer to {key!r}"
            )
        index[folded] = limit


@lru_cache(maxsize=1)
def index() -> dict[str, ImpurityLimit]:
    """Both tables flattened to one name→row index, built once per process.

    One index over both guidelines because the caller's question is "what is the limit for X", and
    knowing that solvents are Q3C and metals are Q3D is precisely the knowledge the agent lacked.

    Public because `tests/test_dataset.py` validates the transcription against itself over the whole
    index — the guideline's own ppm = PDE x 100 identity for Class 2 — and a corpus check that had
    to import a private name is a corpus check nobody writes.
    """
    q3c = read_table(Q3C_DIR, Q3C_FILE, Q3cTable)
    q3d = read_table(Q3D_DIR, Q3D_FILE, Q3dTable)
    built: dict[str, ImpurityLimit] = {}
    for solvent in q3c.solvents:
        note = q3c.classes[solvent.solvent_class]
        limits = [
            LimitValue(
                basis=note.concentration_basis,
                value=solvent.concentration_limit_ppm,
                unit="ppm",
            )
        ]
        if solvent.pde_mg_per_day is not None:
            limits.insert(
                0,
                LimitValue(basis=note.pde_basis, value=solvent.pde_mg_per_day, unit="mg/day"),
            )
        meaning = note.meaning
        if solvent.concern is not None:
            meaning = f"{meaning} Concern for this solvent: {solvent.concern.lower()}."
        _register(
            built,
            [solvent.name, *solvent.synonyms],
            ImpurityLimit(
                substance=solvent.name,
                guideline=q3c.guideline,
                limit_class=f"Class {solvent.solvent_class}",
                class_meaning=meaning,
                limits=limits,
                citation=f"{q3c.guideline}, {note.table}",
            ),
        )
    for element in q3d.elements:
        _register(
            built,
            [element.symbol, element.name, *element.synonyms],
            ImpurityLimit(
                substance=f"{element.name} ({element.symbol})",
                guideline=q3d.guideline,
                limit_class=f"Class {element.element_class}",
                class_meaning=q3d.classes[element.element_class],
                limits=[
                    LimitValue(basis="oral PDE", value=element.oral_pde_ug_per_day, unit="µg/day"),
                    LimitValue(
                        basis="parenteral PDE",
                        value=element.parenteral_pde_ug_per_day,
                        unit="µg/day",
                    ),
                    LimitValue(
                        basis="inhalation PDE",
                        value=element.inhalation_pde_ug_per_day,
                        unit="µg/day",
                    ),
                ],
                citation=f"{q3d.guideline}, {q3d.table}",
            ),
        )
    return built


def impurity_limit(substance: str) -> ImpurityLimitLookup:
    """Look up one substance in the transcribed ICH Q3C / Q3D tables.

    Accepts the guideline's own spelling, an element symbol, an abbreviation a chemist writes, or a
    SMILES: an unmatched query is resolved through the vendored reagent table first, so `THF`,
    `2-MeTHF` and `C1CCOC1` all reach the tetrahydrofuran row without that table's synonyms having
    to be copied into the guideline files.

    A miss returns a lookup whose `limit` is `None`, never a nearby row.
    """
    table = index()
    hit = table.get(_fold(substance))
    if hit is None:
        resolved = resolve_compound_name(substance)
        if resolved is not None:
            hit = table.get(_fold(resolved.name))
    return ImpurityLimitLookup(query=substance, limit=hit)
