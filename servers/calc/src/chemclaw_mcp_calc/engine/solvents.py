"""Which solvent names GFN2-xTB's ALPB model actually has.

`ALPB_SOLVENTS` is the set tblite accepts for `alpb-solvation`: the intersection of its dielectric
table and its Born-parameter table, identical for GFN1 and GFN2. `tests/test_solvents.py`
re-derives it from the installed tblite. Validating up front turns an unknown name such as
"2-MeTHF" into a refusal with suggestions instead of an error deep in the SCF.
"""

from __future__ import annotations

from difflib import get_close_matches

from mcp_server_kit.limits import echo

__all__ = [
    "ALPB_SOLVENTS",
    "SUGGESTED_SOLVENTS",
    "canonical_solvent",
    "did_you_mean",
    "is_supported",
    "require_supported_solvent",
]

# Every name `Calculator.add("alpb-solvation", ...)` accepts, lowercase, aliases included
# (`dichlormethane` is tblite's own spelling). Compared case- and whitespace-insensitively.
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

# What a refusal quotes: one spelling per common process solvent, in polarity order, each in
# `ALPB_SOLVENTS` (asserted in `tests/test_solvents.py`).
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

# Aliases mapped to the one spelling this server keys and sends, so they share a cache row.
# `tests/test_solvents.py` asserts each alias gives its canonical member's ALPB energy exactly
# (which keeps `octanol` and `woctanol` apart). The canonical spelling is the one `xtb --alpb`
# documents, since a spec's solvent goes to both backends.
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

# Spelling suggestions per unknown name; more would read as a second menu.
_MAX_SUGGESTIONS = 3


def _normalize(name: str) -> str:
    """The form `ALPB_SOLVENTS` is keyed in: trimmed and lower-cased, as tblite matches."""
    return name.strip().lower()


def is_supported(name: str) -> bool:
    """Whether GFN2-xTB's ALPB model has parameters for this solvent name."""
    return _normalize(name) in ALPB_SOLVENTS


def did_you_mean(name: str) -> str:
    """A `(did you mean …)` clause for one unknown name, or empty when nothing is close.

    Silent when nothing is close, rather than proposing an unrelated solvent.
    """
    close = get_close_matches(_normalize(name), sorted(ALPB_SOLVENTS), n=_MAX_SUGGESTIONS)
    return f" (did you mean {', '.join(close)}?)" if close else ""


def canonical_solvent(name: str) -> str:
    """The one spelling this server keys and computes `name` under. Refuses an unsupported name.

    Two spellings of one solvent are one calculation, so one key. Idempotent: a canonical member
    maps to itself.

    Raises:
        ValueError: `require_supported_solvent`'s message, so canonicalising never quietly accepts
            an unknown name.
    """
    require_supported_solvent(name)
    normalized = _normalize(name)
    return _CANONICAL.get(normalized, normalized)


def require_supported_solvent(name: str | None) -> None:
    """Refuse a solvent the method has no parameters for, at the edge rather than in the SCF.

    `None` is gas phase and passes. Called from `XtbSpec`'s validator, so it guards both backends —
    the `xtb` binary would otherwise fail minutes later.

    Raises:
        ValueError: naming the solvent, the closest supported spellings, and the common ones.
    """
    if name is None or is_supported(name):
        return
    raise ValueError(
        "GFN2-xTB's ALPB solvation model has no parameters for "
        f"{echo(name)!r}{did_you_mean(name)}. "
        "It is an implicit model with a fixed set of parameterized solvents, so an unlisted one "
        "cannot be approximated — pick the closest supported solvent, or run in the gas phase. "
        f"Commonly used supported solvents: {', '.join(SUGGESTED_SOLVENTS)}."
    )
