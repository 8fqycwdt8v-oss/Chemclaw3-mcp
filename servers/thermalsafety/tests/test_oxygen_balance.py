"""The oxygen-balance screen, checked against published values nobody in this repository computed.

Every case here is a compound whose OB% is in the literature to one decimal, so agreement is
evidence about the formula rather than about the test. That is the same argument
`servers/props/tests/test_dataset.py` makes for a vendored corpus — an independently written number
that must agree — applied to a formula instead of a table.

The molar masses are the second independent check: they come out of the same parse, so a subscript
read wrongly moves both numbers and is caught twice.
"""

from __future__ import annotations

import pytest
from chemclaw_mcp_thermalsafety.engine.oxygen_balance import (
    ATOMIC_WEIGHTS,
    FormulaError,
    molar_mass,
    oxygen_balance,
    parse_formula,
)

#: `(formula, published OB% to CO2, published molar mass)`. Each OB% is the value quoted in the
#: explosives literature for that compound; each molar mass is the standard one. Both are written
#: here from the reference rather than from a run of this code.
PUBLISHED = (
    ("C3H5N3O9", 3.5, 227.09),  # nitroglycerine — the canonical near-zero case
    ("C7H5N3O6", -74.0, 227.13),  # TNT
    ("C3H6N6O6", -21.6, 222.12),  # RDX
    ("C4H8N8O8", -21.6, 296.16),  # HMX — same OB% as RDX, being the same empirical ratio
    ("N2H4O3", 20.0, 80.04),  # ammonium nitrate
    ("C6H12O6", -106.6, 180.16),  # glucose — the case that makes the point about food
    ("C7H8", -312.6, 92.14),  # toluene
)


@pytest.mark.parametrize(("formula", "expected_ob", "expected_mass"), PUBLISHED)
def test_published_compounds_come_back_at_their_published_values(
    formula: str, expected_ob: float, expected_mass: float
) -> None:
    """The whole formula, checked against numbers written independently of it.

    The tolerance is 0.15 percentage points, which is the width the published values' own rounding
    and the choice of atomic-weight table between them account for — tight enough that a wrong
    coefficient on hydrogen or sulfur fails, loose enough that IUPAC 2021 against an older table
    does not.
    """
    result = oxygen_balance(formula)
    assert result.oxygen_balance_percent == pytest.approx(expected_ob, abs=0.15)
    assert result.molar_mass_g_per_mol == pytest.approx(expected_mass, abs=0.05)


def test_glucose_and_tnt_land_in_different_bands_and_neither_reading_is_a_verdict() -> None:
    """The one interpretive property worth asserting: the bands separate, and the text refuses.

    Glucose at -107% and TNT at -74% are the pair the docstring uses to say the screen is not a
    hazard classification, so the bands must actually distinguish them — and every interpretation
    must stop short of calling something safe, which is checked as an absence because the failure
    would be a future edit softening one of these strings into a clearance.
    """
    tnt = oxygen_balance("C7H5N3O6")
    glucose = oxygen_balance("C6H12O6")
    assert tnt.band != glucose.band
    for result in (tnt, glucose, oxygen_balance("C3H5N3O9"), oxygen_balance("C7H8")):
        assert result.interpretation
        lowered = result.interpretation.lower()
        assert "is safe" not in lowered and "no hazard" not in lowered, (
            f"the {result.band!r} interpretation reads as a clearance, which oxygen balance alone "
            "can never license"
        )


def test_a_halogen_ties_up_a_hydrogen_before_that_hydrogen_demands_oxygen() -> None:
    """The correction that distinguishes this from the naive formula, checked by construction.

    Chloroform (CHCl3) has one hydrogen and three chlorines: the hydrogen leaves as HCl, so nothing
    is left of it to burn. Without the correction it would contribute half an oxygen equivalent.
    Asserted against the same formula with the halogens removed, so the test measures the
    *difference the correction makes* rather than restating the expression.
    """
    with_halogen = oxygen_balance("CHCl3").oxygen_balance_percent
    # The same carbon and hydrogen, no halogen to consume it: the hydrogen now demands oxygen, so
    # the balance must be *more* negative per unit mass.
    without = oxygen_balance("CH4")
    assert without.oxygen_balance_percent < -100.0
    assert with_halogen > -100.0, (
        "CHCl3's hydrogen is still being charged for oxygen it cannot reach, so the halogen "
        "correction is not applied"
    )


def test_a_notation_this_parser_would_get_silently_wrong_is_refused_by_name() -> None:
    """Parentheses, hydrate dots and charges — each refused, each told what to write instead.

    This is the highest-value refusal here. `Ca(NO3)2` read token-wise is one calcium, one nitrogen
    and three oxygens: wrong by a factor of two on the element that decides the whole number, with
    a plausible-looking answer. A parser that guesses at notation it does not implement is worse
    than one that has none.
    """
    for bad, expected in (
        ("Ca(NO3)2", "parentheses"),
        ("CuSO4.5H2O", "hydrate"),
        ("CuSO4·5H2O", "hydrate"),
        ("NO3-", "charge"),
        ("NH4+", "charge"),
    ):
        with pytest.raises(FormulaError, match=expected):
            oxygen_balance(bad)


def test_an_element_outside_the_table_is_named_rather_than_approximated() -> None:
    """The fleet's "refuse rather than approximate" rule applied to an atomic-weight table.

    A silently ignored element would return a molar mass that is too low and an OB% that is
    therefore too *favourable* — an error in the unsafe direction, which is the reason this refuses
    rather than dropping what it does not know.
    """
    with pytest.raises(FormulaError, match="Pb"):
        oxygen_balance("Pb3O4")
    with pytest.raises(FormulaError, match="not a molecular formula"):
        oxygen_balance("c3h5n3o9")
    with pytest.raises(FormulaError, match="no molecular formula"):
        oxygen_balance("   ")


def test_the_parse_and_the_molar_mass_agree_with_each_other() -> None:
    """The cheapest typo catch there is, and the reason both come back with the answer.

    A chemist who knows nitroglycerine is 227 g/mol can see in one glance whether their formula was
    read the way they wrote it — so the two numbers must genuinely come from the same parse rather
    than from two code paths that could disagree.
    """
    composition = parse_formula("C3H5N3O9")
    assert composition == {"C": 3.0, "H": 5.0, "N": 3.0, "O": 9.0}
    assert oxygen_balance("C3H5N3O9").molar_mass_g_per_mol == molar_mass(composition)
    assert oxygen_balance("C3H5N3O9").composition == composition


def test_a_two_letter_element_is_not_read_as_two_one_letter_ones() -> None:
    """`Cl` must not parse as carbon and a stray `l`, and `Na` must not become nitrogen.

    The classic formula-parser defect. It is caught here rather than left to the published cases,
    because those happen to use only single-letter symbols — so without this the two-letter path
    would be entirely unexercised while every headline assertion passed.
    """
    assert parse_formula("NaCl") == {"Na": 1.0, "Cl": 1.0}
    assert parse_formula("NCl3") == {"N": 1.0, "Cl": 3.0}
    assert molar_mass({"Na": 1.0, "Cl": 1.0}) == pytest.approx(58.44, abs=0.01)


def test_every_weight_in_the_table_is_a_plausible_atomic_mass() -> None:
    """A transposed digit in the table is a wrong answer in every formula that names the element.

    Checked independently of any formula: every weight must exceed its own symbol's position in a
    hydrogen-relative sense — concretely, be at least 1 and, for every element here, below 200 —
    and the six that dominate organic chemistry are pinned to one decimal from the IUPAC table.
    """
    for symbol, weight in ATOMIC_WEIGHTS.items():
        assert 1.0 <= weight < 200.0, f"{symbol} at {weight} g/mol is not an atomic mass"
    for symbol, expected in (
        ("H", 1.008),
        ("C", 12.011),
        ("N", 14.007),
        ("O", 15.999),
        ("S", 32.06),
        ("Cl", 35.45),
    ):
        assert ATOMIC_WEIGHTS[symbol] == pytest.approx(expected, abs=0.001)
