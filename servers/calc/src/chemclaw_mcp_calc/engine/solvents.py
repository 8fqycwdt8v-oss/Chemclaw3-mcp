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

from chemclaw_mcp_calc.engine.chem import _echo

__all__ = [
    "ALPB_SOLVENTS",
    "SUGGESTED_SOLVENTS",
    "canonical_solvent",
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

# The spellings tblite treats as one solvent, mapped onto the one this server keys and sends.
#
# **Derived by measurement, not by reading the names.** `tests/test_solvents.py` computes the probe
# molecule's ALPB energy for every entry in `ALPB_SOLVENTS` and asserts that each alias gives its
# canonical member's energy *exactly* — which is what makes merging two cache rows into one safe
# rather than plausible. The measurement is also what keeps `octanol` and `woctanol` apart: dry and
# wet octanol differ in the seventh decimal and are two solvents, however alike the names look.
#
# Why it is needed at all: the name is hashed into `params_hash`, so `"water"`, `"Water"`,
# `" water"` and `"h2o"` were four cache rows for one calculation — and on this server's cost
# profile a repeat is minutes to hours, not milliseconds.
#
# The canonical member is the spelling `xtb --alpb` documents, because a spec's solvent is sent to
# **both** backends and only tblite's acceptance is probed here; picking the name the binary's own
# documentation uses is what keeps the canonicalisation from becoming a refusal on the other one.
_CANONICAL = {
    "h2o": "water",
    "mecn": "acetonitrile",
    "carbondisulfide": "cs2",
    "chloroform": "chcl3",
    "diethylether": "ether",
    "dimethylformamide": "dmf",
    "dimethylsulfoxide": "dmso",
    "ethyl acetate": "ethylacetate",
    "furan": "furane",
    "tetrahydrofuran": "thf",
    "dichlormethane": "ch2cl2",
    "dichloromethane": "ch2cl2",
    "methylenechloride": "ch2cl2",
    "n-hexan": "hexane",
    "n-hexane": "hexane",
    "nhexan": "hexane",
    "nhexane": "hexane",
}

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


def canonical_solvent(name: str) -> str:
    """The one spelling this server keys and computes `name` under. Refuses an unsupported name.

    Two spellings of one solvent are one calculation, so they must be one cache key. The membership
    test has always been case- and whitespace-insensitive "because tblite is"; what was missing is
    that the *value* kept on the spec — hashed into `params_hash` and sent to `--alpb` — was the
    caller's original string, so the key distinguished inputs the calculation does not.

    Idempotent by construction: a canonical member maps to itself, which is what lets a spec be
    rebuilt from a spec's own value without drifting.

    Raises:
        ValueError: `require_supported_solvent`'s message, because canonicalising an unknown name
            must not become a second and quieter way to accept one.
    """
    require_supported_solvent(name)
    normalized = _normalize(name)
    return _CANONICAL.get(normalized, normalized)


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
    # **Bounded for the reason `chem._echo` exists, on the argument that was missed.** `_echo`
    # was added because a 3,000-character SMILES produced a 3,018-character refusal that
    # `connector_app` hands to the model verbatim; `solvent` is the *other* caller-controlled
    # string, taken by eleven of this server's tools, and it was still interpolated raw. Measured
    # through the tools, a 900,000-character solvent name produced a **900,636-character**
    # `ValidationError` — 300x the defect the bound was written for — in the context window of the
    # turn that asked.
    raise ValueError(
        f"GFN2-xTB's ALPB solvation model has no parameters for {_echo(name)!r}"
        f"{did_you_mean(name)}. "
        "It is an implicit model with a fixed set of parameterized solvents, so an unlisted one "
        "cannot be approximated — pick the closest supported solvent, or run in the gas phase. "
        f"Commonly used supported solvents: {', '.join(SUGGESTED_SOLVENTS)}."
    )
