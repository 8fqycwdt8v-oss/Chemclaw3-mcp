"""The four unit conversions, against numbers written here from the literature.

They are derived from `scipy.constants`, which ships whatever CODATA edition that scipy release
was built against, so a scipy bump moves every geometry and energy in far decimals.
`engine_version()` therefore names scipy, and the last test holds that.

Tolerances are 1e-8 relative: between a CODATA edition change (~7e-10) and any realistic
transcription error (1e-4 or more).
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

#: `(name, value as computed, value as published)`. Published figures are CODATA 2022, written from
#: the literature rather than from this code. At 1e-8 the 2018 values pass too, by design: this
#: checks the quantity, and `engine_version()` makes an edition change visible.
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
    """Every conversion carries more digits than a transcription would.

    Asserted as "the derived value differs from its own ten-decimal rounding", which fails if a
    derivation is replaced by a truncated literal.
    """
    for what, derived, _ in PUBLISHED:
        assert derived != round(derived, 10), (
            f"{what} carries only ten decimals, which is what a transcribed literal looks like"
        )


def test_the_version_string_names_the_distribution_the_constants_come_from() -> None:
    """A constant derived from a dependency is a constant that dependency can move.

    So scipy is named in the engine version beside tblite and RDKit, making an upgrade a cache miss.
    """
    from importlib.metadata import version

    assert f"scipy-{version('scipy')}" in engine_version()
    assert f"tblite-{version('tblite')}" in engine_version()
    assert f"rdkit-{version('rdkit')}" in engine_version()


def test_the_engine_string_does_not_name_a_program_the_engine_does_not_run() -> None:
    """The engine string does not name geomeTRIC, a program the engine does not run.

    `engine_version()` keys `xtb.sp`, `xtb.properties`, `xtb.fukui` and `xtb.hess`, none of which
    optimizes. geomeTRIC belongs in `OptSpec.calc_version()`, held by the optimization-version test.
    An absence test fails whoever widens the shared string instead.
    """
    assert "geometric" not in engine_version(), (
        "geomeTRIC is in engine_version(), which keys four calculations that never run it; it "
        "belongs in OptSpec.calc_version(), the one task whose payload it decides"
    )
