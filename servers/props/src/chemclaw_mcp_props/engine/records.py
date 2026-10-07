"""The solvent table: what a row is, and how a chemist's spelling finds one.

Lookup is by any name a chemist would type (`2-MeTHF`, `MeTHF`, `2-methyltetrahydrofuran`). An
unrecognised name returns nothing rather than the nearest match: a silently substituted solvent is
worse than no answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal

from mcp_server_kit import Dataset, load_dataset, read_records
from mcp_server_kit.limits import echo

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

Miscibility = Literal["miscible", "partial", "immiscible"]
GreennessBand = Literal["recommended", "problematic", "hazardous", "highly_hazardous"]


@dataclass(frozen=True, slots=True)
class Solvent:
    """One solvent, exactly as the vendored table records it.

    Optional fields are absent rather than zero (dichloromethane *has* no flash point), so every
    consumer must handle `None` explicitly.
    """

    name: str
    aliases: tuple[str, ...]
    cas: str
    smiles: str
    formula: str
    mw: float
    bp_c: float
    mp_c: float
    density_20c: float
    dielectric: float
    hansen_d: float
    hansen_p: float
    hansen_h: float
    water_miscibility: Miscibility
    peroxide_former: bool
    ich_class: str
    greenness_band: GreennessBand
    hazard_flags: tuple[str, ...] = ()
    flash_point_c: float | None = None
    ich_limit_ppm: float | None = None
    antoine: tuple[float, float, float] | None = None
    # The temperature window, in kelvin, the Antoine constants were fitted over; `correlations`
    # refuses outside it and falls back.
    antoine_range_k: tuple[float, float] | None = None
    hvap_kj_mol: float | None = None
    # Filled by `_index` so every answer can name its own source without the caller threading it.
    provenance: str = field(default="", compare=False)


def _optional_float(raw: str) -> float | None:
    """Parse a cell that is allowed to be blank. Blank means absent, never zero."""
    text = raw.strip()
    return float(text) if text else None


def _split(raw: str) -> tuple[str, ...]:
    """Split a semicolon-delimited cell, dropping blanks."""
    return tuple(part.strip() for part in raw.split(";") if part.strip())


def _row_to_solvent(row: dict[str, str], provenance: str) -> Solvent:
    """Build one `Solvent` from a CSV row, failing loudly on a malformed required column."""
    # A, B, C and the fitted range are one unit: a row missing any of the five carries no fit and
    # answers through the Clausius-Clapeyron route.
    antoine_keys = ("antoine_a", "antoine_b", "antoine_c", "antoine_tmin_k", "antoine_tmax_k")
    antoine_parts = [_optional_float(row[key]) for key in antoine_keys]
    complete = all(part is not None for part in antoine_parts)
    antoine = (antoine_parts[0], antoine_parts[1], antoine_parts[2]) if complete else None
    antoine_range = (antoine_parts[3], antoine_parts[4]) if complete else None
    return Solvent(
        name=row["name"].strip(),
        aliases=_split(row["aliases"]),
        cas=row["cas"].strip(),
        smiles=row["smiles"].strip(),
        formula=row["formula"].strip(),
        mw=float(row["mw"]),
        bp_c=float(row["bp_c"]),
        mp_c=float(row["mp_c"]),
        density_20c=float(row["density_20c"]),
        dielectric=float(row["dielectric"]),
        hansen_d=float(row["hansen_d"]),
        hansen_p=float(row["hansen_p"]),
        hansen_h=float(row["hansen_h"]),
        water_miscibility=row["water_miscibility"].strip(),  # type: ignore[arg-type]
        peroxide_former=row["peroxide_former"].strip().lower() == "yes",
        ich_class=row["ich_class"].strip(),
        greenness_band=row["greenness_band"].strip(),  # type: ignore[arg-type]
        hazard_flags=_split(row["hazard_flags"]),
        flash_point_c=_optional_float(row["flash_point_c"]),
        ich_limit_ppm=_optional_float(row["ich_limit_ppm"]),
        antoine=antoine,  # type: ignore[arg-type]
        antoine_range_k=antoine_range,  # type: ignore[arg-type]
        hvap_kj_mol=_optional_float(row["hvap_kj_mol"]),
        provenance=provenance,
    )


def _key(name: str) -> str:
    """Normalise a name for lookup by dropping everything that is not a letter or a digit, lowercased.
    """
    return re.sub(r"[^a-z0-9]", "", name.lower())


@lru_cache(maxsize=1)
def dataset() -> Dataset:
    """The vendored dataset manifest, verified against its checksum on first use."""
    return load_dataset(DATA_DIR)


@lru_cache(maxsize=1)
def _index() -> tuple[tuple[Solvent, ...], dict[str, Solvent]]:
    """Every solvent, plus the lookup map from every spelling to its row.

    A duplicate key is a hard error, so an answer can never depend on file order.
    """
    loaded = dataset()
    solvents = tuple(
        _row_to_solvent(row, provenance=loaded.citation()) for row in read_records(loaded)
    )
    lookup: dict[str, Solvent] = {}
    for solvent in solvents:
        for spelling in (solvent.name, solvent.cas, *solvent.aliases):
            key = _key(spelling)
            if key in lookup and lookup[key] is not solvent:
                raise ValueError(
                    f"the solvent table maps {spelling!r} to both {lookup[key].name!r} and "
                    f"{solvent.name!r}; a name that resolves two ways resolves neither"
                )
            lookup[key] = solvent
    return solvents, lookup


def all_solvents() -> tuple[Solvent, ...]:
    """Every row of the table, in file order."""
    return _index()[0]


def find(name: str) -> Solvent | None:
    """The solvent a chemist means by `name`, or `None` if the table does not have it."""
    return _index()[1].get(_key(name))


def require(name: str) -> Solvent:
    """`find`, but raising the message the agent should see when the name is unknown.

    Raises:
        ValueError: with the closest spellings the table does know.
    """
    found = find(name)
    if found is not None:
        return found
    known = ", ".join(sorted(solvent.name for solvent in all_solvents())[:8])
    raise ValueError(
        f"{echo(name)!r} is not in the vendored solvent table ({len(all_solvents())} solvents; "
        f"e.g. {known}, ...). Call list_solvents to see the full set — this server answers from a "
        "fixed corpus and cannot look a solvent up anywhere else."
    )
