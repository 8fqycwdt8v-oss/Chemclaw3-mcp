"""The four unit conversions, against numbers written here from the literature.

They used to be literals carrying the comment "CODATA 2018, to full double precision", and that was
false of the first: CODATA-2018's Bohr radius gives 1.8897261246257702 and the literal stopped at
1.8897261246 — eleven significant digits where a float64 holds about seventeen. They are now derived
from `scipy.constants`, which is already a declared dependency of this server.

**Deriving moved the problem rather than removing it, and this file is where that is said.**
`scipy.constants` is not a fixed table: it ships whatever CODATA edition that release of scipy was
built against, and scipy 1.17.1 ships **CODATA 2022** (a0 = 5.29177210544e-11) where the literals
claimed 2018 (5.29177210903e-11). So the values moved when they were derived — 6.9e-10 relative on
the length conversion — and they will move again when scipy ships CODATA 2026. That is a change to
every geometry and every energy this server computes, in far decimals, with nothing in a cache key
to show for it: the exact failure `engine/key.py` exists to prevent.

The answer is `engine_version()`, which now names the installed scipy beside tblite and RDKit, and
the last test here is what holds it.

The tolerances below are **1e-8 relative**, chosen to sit between the two things that must be told
apart: a CODATA edition change moves these by ~7e-10, and any realistic transcription error — a
dropped digit, a transposition, the wrong constant entirely — moves them by 1e-4 or more.
"""

from __future__ import annotations

import pytest
from chemclaw_mcp_calc.engine.xtb_engine import (
    ANGSTROM_TO_BOHR,
    AU_TO_DEBYE,
    HARTREE_TO_KCAL,
    engine_version,
)
from chemclaw_mcp_calc.engine.xtb_props import _HARTREE_TO_EV

#: `(name, value as computed, value as published)`. Every published figure is written here from the
#: literature rather than from a run of this code, which is what makes agreement evidence about the
#: derivation rather than about the test. They are the CODATA 2022 values, because that is what the
#: pinned scipy ships; at 1e-8 the 2018 values would pass too, and that is deliberate — this file
#: checks that the conversion is the right *quantity*, and `engine_version()` is what makes a change
#: of edition visible.
PUBLISHED = (
    ("1 angstrom in bohr", ANGSTROM_TO_BOHR, 1.8897261259),
    ("1 hartree in kcal/mol", HARTREE_TO_KCAL, 627.5094740),
    ("1 atomic unit of dipole in debye", AU_TO_DEBYE, 2.5417464715),
    ("1 hartree in eV", _HARTREE_TO_EV, 27.211386246),
)


@pytest.mark.parametrize(("what", "derived", "published"), PUBLISHED, ids=[r[0] for r in PUBLISHED])
def test_the_derived_conversion_is_the_published_one(
    what: str, derived: float, published: float
) -> None:
    """One conversion, against a number nobody in this repository computed."""
    assert derived == pytest.approx(published, rel=1e-8), what


def test_every_conversion_carries_more_digits_than_a_transcription_did() -> None:
    """The specific defect: a literal truncated to eleven significant digits.

    `1.8897261246` is the value that shipped, and CODATA-2018's own Bohr radius gives
    `1.8897261246257702`. A `float` holds about seventeen significant digits, so the literal threw
    away six of them for no reason — and, worse, the comment above it claimed it did not.

    Asserted as "the derived value differs from its own ten-decimal rounding", which is what a
    re-transcription would not. It is the weakest possible statement of the property and it is
    the one that fails if somebody replaces a derivation with a literal again.
    """
    for what, derived, _ in PUBLISHED:
        assert derived != round(derived, 10), (
            f"{what} carries only ten decimals, which is what a transcribed literal looks like"
        )


def test_the_version_string_names_the_distribution_the_constants_come_from() -> None:
    """A constant derived from a dependency is a constant that dependency can move.

    tblite and RDKit have been in this string since it was written, for the same reason in each
    case: an upgrade shifts numbers, so it must be a cache miss on Chemclaw3's side rather than a
    stale hit. scipy joined them the moment these four conversions stopped being literals — and it
    had been a dependency for years before that without belonging here, which is why it is worth
    asserting rather than assuming somebody will remember.
    """
    from importlib.metadata import version

    assert f"scipy-{version('scipy')}" in engine_version()
    assert f"tblite-{version('tblite')}" in engine_version()
    assert f"rdkit-{version('rdkit')}" in engine_version()
