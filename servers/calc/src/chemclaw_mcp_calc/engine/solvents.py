"""Which solvent names GFN2-xTB's ALPB model actually has.

Ported from Chemclaw3's `chemclaw/science/calc/solvents.py`, **minus its reason for being a
separate module there**. Over there this file exists so `JobSpec.precondition` can refuse a durable
solvent screen *before* a workflow starts, without dragging `tblite` into the chat service's
process. There are no durable jobs here and no import-isolation constraint, so what survives is the
data and the two functions the error messages need.

**The names were measured, not recalled.** `ALPB_SOLVENTS` is every name tblite accepts for
`alpb-solvation`, obtained by probing the solvent-name table compiled into `_libtblite` against a
live `Calculator`. That distinction matters, because tblite has *two* tables and rejects a name
from each with a different message: a name absent from the dielectric database fails with "String
value for epsilon was not found" (`2-methyltetrahydrofuran`, `mtbe`), while a name present there
but lacking Born parameters for the Hamiltonian fails with "No ALPB/GBSA parameters found for the
method/solvent" (`heptane`, `cyclohexane`, `xylene`). Only the intersection works, and it is
identical for GFN1-xTB and GFN2-xTB — so the set does not depend on the method and is written here
as one constant rather than a per-method map that would have one entry twice.
`tests/test_solvents.py` re-derives it against the installed tblite, so an upgrade that adds or
drops a solvent fails a test instead of surfacing as a wrong refusal.

The live failure this exists for: a chemist asked for "2-MeTHF" — among the most common process
solvents there is — and the calculation died deep inside on tblite's `String value for epsilon was
not found among database of solvents`. Nothing was wrong with the plumbing; the name was simply not
a name the method knows.
"""

from __future__ import annotations

from difflib import get_close_matches

__all__ = [
    "ALPB_SOLVENTS",
    "SUGGESTED_SOLVENTS",
    "did_you_mean",
    "is_supported",
    "require_supported_solvent",
]

# Every name `Calculator.add("alpb-solvation", ...)` accepts, lowercase. Aliases are included
# because a chemist and a model both write them: `h2o`, `mecn`, `nhexane` and `dichlormethane` (sic
# — tblite's own spelling) are all real keys, not typos this module should be normalising away.
# Comparison is case-insensitive and whitespace-trimmed (`_normalize`) because tblite is.
ALPB_SOLVENTS = frozenset(
    {
        "acetone",
        "acetonitrile",
        "aniline",
        "benzaldehyde",
        "benzene",
        "carbondisulfide",
        "ch2cl2",
        "chcl3",
        "chloroform",
        "cs2",
        "dichlormethane",
        "dichloromethane",
        "diethylether",
        "dimethylformamide",
        "dimethylsulfoxide",
        "dioxane",
        "dmf",
        "dmso",
        "ethanol",
        "ether",
        "ethyl acetate",
        "ethylacetate",
        "furan",
        "furane",
        "h2o",
        "hexadecane",
        "hexane",
        "mecn",
        "methanol",
        "methylenechloride",
        "n-hexan",
        "n-hexane",
        "nhexan",
        "nhexane",
        "nitromethane",
        "octanol",
        "phenol",
        "tetrahydrofuran",
        "thf",
        "toluene",
        "water",
        "woctanol",
    }
)

# What a refusal quotes: one canonical spelling per distinct solvent a process chemist reaches for,
# in polarity order so the list reads as a range rather than an alphabet. Every entry is in
# `ALPB_SOLVENTS` by construction (asserted in `tests/test_solvents.py`), so this can never again
# advertise a name the method rejects, nor silently omit one it supports — the aliases are what is
# left out, deliberately, since naming `h2o` beside `water` spends a line saying nothing.
SUGGESTED_SOLVENTS = (
    "water",
    "methanol",
    "ethanol",
    "acetonitrile",
    "dmso",
    "dmf",
    "acetone",
    "thf",
    "dioxane",
    "ethylacetate",
    "ch2cl2",
    "chcl3",
    "toluene",
    "benzene",
    "ether",
    "hexane",
)

# How many spelling suggestions a single unknown name earns. Three is the point where the list stops
# reading as "did you mean this?" and starts reading as a second menu — `SUGGESTED_SOLVENTS` is
# already the menu, and the message carries both.
_MAX_SUGGESTIONS = 3


def _normalize(name: str) -> str:
    """The form `ALPB_SOLVENTS` is keyed in: tblite matches case-insensitively and trims, so do we.

    Written once rather than inlined at both call sites, because a membership test and an error
    message that disagreed about normalisation would refuse a name and then fail to explain why.
    """
    return name.strip().lower()


def is_supported(name: str) -> bool:
    """Whether GFN2-xTB's ALPB model has parameters for this solvent name."""
    return _normalize(name) in ALPB_SOLVENTS


def did_you_mean(name: str) -> str:
    """A `(did you mean …)` clause for one unknown name, or empty when nothing is close.

    Worth the four lines because the single measured failure this module exists for —
    "2-methyltetrahydrofuran" — is one edit family away from `tetrahydrofuran`, which is both the
    closest supported solvent and very often the right substitution for a chemist who reached for
    2-MeTHF. Silence when nothing matches, rather than a floor-scraping guess: proposing `phenol`
    for `mtbe` would be worse than proposing nothing.
    """
    close = get_close_matches(_normalize(name), sorted(ALPB_SOLVENTS), n=_MAX_SUGGESTIONS)
    return f" (did you mean {', '.join(close)}?)" if close else ""


def require_supported_solvent(name: str | None) -> None:
    """Refuse a solvent the method has no parameters for, at the edge rather than in the SCF.

    Gas phase is not a solvent and is spelled `None`, so it passes untouched.

    Called from `XtbSpec`'s validator, which is what puts the check in front of **both** backends:
    `xtb_engine.make_calculator` catches an unknown name from tblite, but the `xtb` binary would
    take `--alpb 2-methyltetrahydrofuran` and fail minutes later inside a subprocess. One
    shortlist, one message, either route.

    Raises:
        ValueError: naming the solvent, the closest supported spellings, and the common ones —
            everything the chemist needs to correct the call in the same turn.
    """
    if name is None or is_supported(name):
        return
    raise ValueError(
        f"GFN2-xTB's ALPB solvation model has no parameters for {name!r}{did_you_mean(name)}. "
        "It is an implicit model with a fixed set of parameterized solvents, so an unlisted one "
        "cannot be approximated — pick the closest supported solvent, or run in the gas phase. "
        f"Commonly used supported solvents: {', '.join(SUGGESTED_SOLVENTS)}."
    )
