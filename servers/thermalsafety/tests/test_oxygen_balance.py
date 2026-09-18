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
from chemclaw_mcp_thermalsafety.engine import selftest
from chemclaw_mcp_thermalsafety.engine.oxygen_balance import (
    ALLOWED_ELEMENTS,
    ATOMIC_WEIGHTS,
    FormulaError,
    molar_mass,
    oxygen_balance,
    parse_formula,
)
from molmass import Formula

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
        ("[H2O]2", "brackets"),
        ("{H2O}2", "braces"),
        ("CuSO4.5H2O", "hydrate"),
        ("CuSO4·5H2O", "hydrate"),
        ("NO3-", "charge"),
        ("NH4+", "charge"),
    ):
        with pytest.raises(FormulaError, match=expected):
            oxygen_balance(bad)


def test_every_grouping_bracket_is_refused_and_not_just_the_two_somebody_listed() -> None:
    """A brace group was expanded by the library while its two siblings were refused by name.

    Measured at `6c6a0eb`: `parse_formula("{H2O}2")` returned `{'H': 4.0, 'O': 2.0}` — the correct
    expansion, produced by `molmass` rather than by anything reviewed here — while `(H2O)2` and
    `[H2O]2` were refused. The pre-`molmass` parser refused all three because its regex admitted
    nothing but element symbols and digits; the blocklist that replaced it enumerated two pairs of
    three (`D-2026-09-16-a-refusal-set-with-a-hole-in-it-is-not-a-refusal-set`).

    Driven over the three pairs as *characters* rather than over three example strings, because the
    defect was a missing row in a table and an example-by-example test is the same table written a
    second time. Each is confirmed to be something the library would otherwise answer, so the
    refusal is guarding a real delegation rather than a case molmass refuses anyway.
    """
    for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
        grouped = f"{opening}H2O{closing}2"
        assert Formula(grouped).mass > 0, (
            f"{grouped!r} is no longer parsed by molmass, so refusing it guards nothing"
        )
        with pytest.raises(FormulaError, match="refuses rather than guesses"):
            parse_formula(grouped)


def test_the_published_constants_version_and_digest_both_see_the_adopted_table() -> None:
    """A version that cannot see its own table is not a version, and neither is a digest.

    `CONSTANTS_VERSION` is hand-bumped and `_constants_digest()` hashes `oxygen_balance.py`. Since
    the weights moved to `molmass`, neither covers a weight: the module holds a comprehension, and
    `uv.lock` resolves **two** molmass releases on purpose (2026.1.8 below Python 3.12, 2026.8.15
    at or above it), so two pods can legitimately serve different tables
    (`D-2026-09-16-a-version-that-cannot-see-its-own-table-is-not-a-version`).

    Asserted in both directions, because the version half alone would pass on a digest that still
    ignored the numbers:

    - the published version names the installed molmass distribution;
    - the digest **moves** when a weight moves, which is what a checksum is for. Driven by
      substituting one weight rather than by comparing two literals — a pinned digest would have to
      be re-transcribed on every unrelated edit to that module, and would then be pinning the file
      rather than the table.
    """
    from importlib.metadata import version as _distribution_version

    published = selftest.CONSTANTS_VERSION
    assert f"molmass-{_distribution_version('molmass')}" in published, (
        f"{published!r} does not name the distribution the atomic weights come from, so two pods "
        "holding the two molmass releases uv.lock resolves publish the same version string"
    )

    before = selftest._constants_digest()
    original = dict(ATOMIC_WEIGHTS)
    try:
        ATOMIC_WEIGHTS["C"] = original["C"] + 0.01
        assert selftest._constants_digest() != before, (
            "a changed atomic weight left the digest unmoved, so /healthz cannot tell two tables "
            "apart — which is the one question a digest exists to answer that a version cannot"
        )
    finally:
        ATOMIC_WEIGHTS.clear()
        ATOMIC_WEIGHTS.update(original)
    assert selftest._constants_digest() == before


def test_the_two_widenings_molmass_brought_are_the_ones_that_were_argued() -> None:
    """Adopting a library moves a boundary, and a boundary that moved silently is the finding.

    Both of these are changes from the hand-written parser and both are recorded in
    `parse_formula`'s docstring rather than left to be rediscovered:

    - **whitespace inside a formula now parses**, because molmass ignores it. Kept: the answer is
      the one the chemist meant, and a copy-pasted `C6 H5 NO2` is not a notation this screen would
      get wrong.
    - **an explicit zero count is now refused**, where the old parser returned a zero. Kept for the
      opposite reason: it is the stricter direction, and an element written with a count of nothing
      is a typo rather than a composition.
    """
    assert parse_formula("C6 H5 NO2") == parse_formula("C6H5NO2")
    assert parse_formula("  C7H5N3O6  ") == {"C": 7.0, "H": 5.0, "N": 3.0, "O": 6.0}
    for zeroed in ("C0", "C1H0"):
        with pytest.raises(FormulaError):
            parse_formula(zeroed)


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
    """A wrong weight is a wrong answer in every formula that names the element.

    The table is no longer transcribed — `ATOMIC_WEIGHTS` is read from `molmass` over
    `ALLOWED_ELEMENTS` — so this check changed subject rather than losing its point. It is now an
    *independent* statement of the six weights that dominate organic chemistry, written here from
    the IUPAC 2021 conventional table, against a library's own table: two sources that were
    compiled separately and must agree.

    **The tolerance is 0.005 g/mol and it is a measurement, not a round number.** Measured against
    molmass 2026.1.8, four of the six agree to better than 5e-4 and two do not: sulfur is 32.0648
    where IUPAC 2021 gives the conventional 32.06, and chlorine is 35.4529 against 35.45. Those are
    the older standard atomic weights rather than the conventional values IUPAC publishes for
    elements with a natural-abundance interval — a real difference between two defensible tables,
    not an error in either, and one worth knowing about rather than hiding behind a loose bound.
    0.005 covers it with nothing to spare, and is still an order of magnitude below what any
    realistic corruption moves a weight by: carbon transposed to 12.101 is 0.09 out.

    What that difference costs the answers is bounded a second time by
    `test_published_compounds_come_back_at_their_published_values`, whose 0.15-point tolerance is
    forty times the largest OB% change the whole table swap produced.
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
        assert ATOMIC_WEIGHTS[symbol] == pytest.approx(expected, abs=0.005)


def test_the_table_holds_exactly_the_elements_this_screen_is_reviewed_for() -> None:
    """The allowlist is the policy; the library is only where the numbers come from.

    `molmass` knows all 109 elements, so nothing about importing it narrows what could be parsed —
    the narrowing is `ALLOWED_ELEMENTS` and this is the assertion that it still does the work. A
    future edit that derived the table from the library's own symbol list instead would widen the
    screen's reviewed domain to the whole periodic table without a line saying so.
    """
    assert set(ATOMIC_WEIGHTS) == set(ALLOWED_ELEMENTS)
    assert len(ALLOWED_ELEMENTS) == 17
    assert "Pb" not in ALLOWED_ELEMENTS


def test_a_notation_molmass_would_answer_is_still_refused_here() -> None:
    """The refusals run in front of the library, which is the only reason they still exist.

    Each of these is a string `molmass.Formula` parses without complaint — measured against molmass
    2026.1.8, `Ca(NO3)2` gives 164.09 g/mol, `CuSO4.5H2O` gives 249.68, and `2H2O` gives **deuterium
    oxide** rather than two waters. Delegating the grammar would therefore have turned three
    deliberate refusals into three confident wrong answers, and the last one silently: a chemist
    who writes a stoichiometric coefficient does not mean a mass number.

    Driven through the library here as well as through `parse_formula`, so that the day molmass
    stops parsing one of them this test says the premise changed rather than passing for a new
    reason.
    """
    for notation in ("Ca(NO3)2", "CuSO4.5H2O", "2H2O"):
        assert Formula(notation).mass > 0, (
            f"{notation!r} is no longer parsed by molmass, so the refusal above it is now "
            "guarding a case the library would refuse anyway — re-read the argument"
        )
        with pytest.raises(FormulaError):
            parse_formula(notation)


def test_an_isotope_symbol_is_named_rather_than_silently_weighed() -> None:
    """`D2O` reaches the allowlist as `2H`, which is not an element this screen carries.

    Worth its own case because the refusal comes from a different place than the others: there is
    no `D` character to reject up front, so this is the allowlist catching a symbol the library
    invented during the parse. The message must name what it saw.
    """
    with pytest.raises(FormulaError, match="2H"):
        parse_formula("D2O")
