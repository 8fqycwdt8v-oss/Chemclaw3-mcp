"""The oxygen-balance screen, checked against published values nobody in this repository computed.

Each compound's OB% is in the literature to one decimal, so agreement is evidence about the
formula. Molar masses come from the same parse, so a misread subscript is caught twice.
"""

from __future__ import annotations

import re

import pytest
from chemclaw_mcp_thermalsafety.engine import oxygen_balance as oxygen_balance_module
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
    """Published compounds come back at their published OB% and molar mass.

    The 0.15-point tolerance spans published rounding and atomic-weight table choice: tight enough
    to fail a wrong hydrogen or sulfur coefficient, loose enough for IUPAC 2021 against older
    tables.
    """
    result = oxygen_balance(formula)
    assert result.oxygen_balance_percent == pytest.approx(expected_ob, abs=0.15)
    assert result.molar_mass_g_per_mol == pytest.approx(expected_mass, abs=0.05)


def test_glucose_and_tnt_land_in_different_bands_and_neither_reading_is_a_verdict() -> None:
    """Glucose and TNT land in different bands, and no interpretation calls anything safe.

    The safe-language check is an absence, guarding against a future edit softening a band's text
    into a clearance.
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
    """A halogen ties up a hydrogen as HX before that hydrogen demands oxygen.

    Asserted against the same formula with the halogens removed, so the test measures the difference
    the correction makes rather than restating the expression.
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
    """Parentheses, hydrate dots and charges are each refused, naming what to write instead.

    `Ca(NO3)2` read token-wise is wrong by a factor of two on oxygen with a plausible answer;
    guessing at unimplemented notation is worse than refusing.
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
    """Every grouping bracket pair is refused, not just two of them.

    Driven over the bracket characters rather than example strings, and each is confirmed to be
    something `molmass` would otherwise expand, so the refusal guards a real delegation.
    """
    for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
        grouped = f"{opening}H2O{closing}2"
        assert Formula(grouped).mass > 0, (
            f"{grouped!r} is no longer parsed by molmass, so refusing it guards nothing"
        )
        with pytest.raises(FormulaError, match="refuses rather than guesses"):
            parse_formula(grouped)


def test_the_published_constants_version_and_digest_both_see_the_adopted_table() -> None:
    """The published constants version and digest both see the adopted weight table.

    Weights come from `molmass`, and `uv.lock` resolves different molmass releases per Python, so
    two pods may serve different tables. Asserted both ways: the version names the installed molmass
    distribution, and the digest moves when a weight is substituted (not compared to a pinned
    literal, which would pin the file rather than the table).
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
    """The two boundary changes `molmass` brought are the argued ones.

    Whitespace inside a formula parses (a copy-pasted `C6 H5 NO2` means what it says); an explicit
    zero count is refused (a typo, and the stricter direction). Both are documented in
    `parse_formula`.
    """
    assert parse_formula("C6 H5 NO2") == parse_formula("C6H5NO2")
    assert parse_formula("  C7H5N3O6  ") == {"C": 7.0, "H": 5.0, "N": 3.0, "O": 6.0}
    for zeroed in ("C0", "C1H0"):
        with pytest.raises(FormulaError):
            parse_formula(zeroed)


def test_an_element_outside_the_table_is_named_rather_than_approximated() -> None:
    """An element outside the table is named rather than approximated.

    Ignoring it would understate the molar mass and make OB% too favourable: an error in the unsafe
    direction.
    """
    with pytest.raises(FormulaError, match="Pb"):
        oxygen_balance("Pb3O4")
    with pytest.raises(FormulaError, match="not a molecular formula"):
        oxygen_balance("c3h5n3o9")
    with pytest.raises(FormulaError, match="no molecular formula"):
        oxygen_balance("   ")


def test_the_parse_and_the_molar_mass_agree_with_each_other() -> None:
    """The parse and the molar mass agree with each other.

    A chemist checks the molar mass at a glance to see the formula was read as written, so both
    numbers must come from the same parse.
    """
    composition = parse_formula("C3H5N3O9")
    assert composition == {"C": 3.0, "H": 5.0, "N": 3.0, "O": 9.0}
    assert oxygen_balance("C3H5N3O9").molar_mass_g_per_mol == molar_mass(composition)
    assert oxygen_balance("C3H5N3O9").composition == composition


def test_a_two_letter_element_is_not_read_as_two_one_letter_ones() -> None:
    """`Cl` does not parse as carbon plus `l`, and `Na` does not become nitrogen.

    The published cases use only single-letter symbols, so without this the two-letter path would
    be unexercised.
    """
    assert parse_formula("NaCl") == {"Na": 1.0, "Cl": 1.0}
    assert parse_formula("NCl3") == {"N": 1.0, "Cl": 3.0}
    assert molar_mass({"Na": 1.0, "Cl": 1.0}) == pytest.approx(58.44, abs=0.01)


def test_every_weight_in_the_table_is_a_plausible_atomic_mass() -> None:
    """Every weight in the table is a plausible atomic mass.

    `ATOMIC_WEIGHTS` is read from `molmass`; this checks six dominant weights against an independent
    statement from the IUPAC 2021 conventional table. The 0.005 g/mol tolerance covers molmass's
    older standard values for S and Cl, a real difference between defensible tables, while staying
    far below a realistic corruption.
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
    """The table holds exactly the elements this screen is reviewed for.

    `molmass` knows every element; `ALLOWED_ELEMENTS` is the narrowing, and deriving the table from
    the library's symbol list would silently widen the reviewed domain.
    """
    assert set(ATOMIC_WEIGHTS) == set(ALLOWED_ELEMENTS)
    assert len(ALLOWED_ELEMENTS) == 17
    assert "Pb" not in ALLOWED_ELEMENTS


def test_a_notation_molmass_would_answer_is_still_refused_here() -> None:
    """Notations `molmass` would answer are still refused here.

    The library parses `Ca(NO3)2`, hydrate dots, and reads `2H2O` as deuterium oxide, so delegating
    the grammar would turn refusals into confident wrong answers. The premise is driven through the
    library too, so if it stops parsing one the test reports that the premise changed.
    """
    for notation in ("Ca(NO3)2", "CuSO4.5H2O", "2H2O"):
        assert Formula(notation).mass > 0, (
            f"{notation!r} is no longer parsed by molmass, so the refusal above it is now "
            "guarding a case the library would refuse anyway — re-read the argument"
        )
        with pytest.raises(FormulaError):
            parse_formula(notation)


def test_an_isotope_symbol_is_named_rather_than_silently_weighed() -> None:
    """`D2O` is refused by naming `D`, before `molmass` can turn it into `2H`.

    The symbol pass in front of the library sees the `D` the caller wrote, so the message names
    what the caller typed rather than what the library would have invented.
    """
    with pytest.raises(FormulaError, match="'D'"):
        parse_formula("D2O")


@pytest.mark.parametrize(
    ("typed", "named"),
    [("TNT", "'T'"), ("PETN", "'E'"), ("THF", "'T'"), ("Et2O", "'Et'"), ("Me", "'Me'")],
)
def test_a_name_or_abbreviation_typed_for_a_formula_is_refused_not_expanded(
    typed: str, named: str
) -> None:
    """A name or abbreviation typed for a formula is refused, not expanded.

    `molmass` expands acronyms and residue codes, which turned `PETN` and `TNT` into unrelated
    compositions with reassuring OB%. The premise is driven through the library too.
    """
    assert Formula(typed).mass > 0
    with pytest.raises(FormulaError, match=named):
        oxygen_balance(typed)


@pytest.mark.parametrize(
    ("typed", "stands_for"),
    [
        ("BPO", "benzoyl peroxide"),
        ("CHP", "cumene hydroperoxide"),
        ("NC", "nitrocellulose"),
        ("NaN", "missing number"),
    ],
)
def test_an_acronym_spelled_from_real_element_symbols_is_refused_by_name(
    typed: str, stands_for: str
) -> None:
    """An acronym spelled from real element symbols is refused by name.

    `BPO`, `CHP`, `NC`, `NaN` pass the symbol check and would yield an OB% for an unrelated
    composition. The premise is driven through this parser's own symbol check.
    """
    assert all(symbol in ALLOWED_ELEMENTS for symbol in re.findall(r"[A-Z][a-z]?", typed)), (
        "the premise is that every symbol is an element; the symbol check would refuse this anyway"
    )
    with pytest.raises(FormulaError, match=stands_for):
        oxygen_balance(typed)


def test_a_real_formula_made_only_of_one_letter_symbols_still_answers() -> None:
    """The acronym refusal is a list rather than a shape rule, since a shape rule refuses these."""
    for formula in ("HCN", "COS", "CO", "NO"):
        assert oxygen_balance(formula).composition


def test_the_formula_those_names_stand_for_still_answers() -> None:
    """The refusal is of the notation, not the compound: TNT written as C7H5N3O6 is about -74%."""
    assert oxygen_balance("C7H5N3O6").oxygen_balance_percent == pytest.approx(-74.0, abs=0.5)


#: `(formula, the token an interpretation string quotes, the band that string belongs to)`.
#:
#: Every percentage a band's interpretation quotes is a claim about this module's arithmetic: the
#: token must appear in that band's text, and recomputing the formula must land in the band and
#: round to the token.
QUOTED_IN_INTERPRETATIONS = (
    ("KClO4", "+40%", "oxygen-rich"),
    ("H2O2", "+47%", "oxygen-rich"),
    ("C3H5N3O9", "+3.5%", "near-balanced"),
    ("N2H4O3", "+20%", "near-balanced"),
    ("C7H5N3O6", "-74%", "moderately oxygen-deficient"),
    ("C6H12O6", "-107%", "oxygen-deficient"),
    ("C12H22O11", "-112%", "oxygen-deficient"),
    ("C7H8", "-313%", "strongly oxygen-deficient"),
    ("CH4", "-399%", "strongly oxygen-deficient"),
)


@pytest.mark.parametrize(("formula", "token", "band"), QUOTED_IN_INTERPRETATIONS)
def test_a_compound_an_interpretation_quotes_lands_in_the_band_that_quotes_it(
    formula: str, token: str, band: str
) -> None:
    """A compound an interpretation quotes lands in the band that quotes it, at the quoted value.

    The tolerance is half the token's last digit, derived rather than chosen, and the token is kept
    as a string so the substring check fails when the text moves.
    """
    quoted = float(token.rstrip("%"))
    places = len(token.rstrip("%").split(".")[1]) if "." in token else 0
    tolerance = 0.5 * 10.0**-places

    result = oxygen_balance(formula)
    assert result.band == band, (
        f"{formula} computes to {result.oxygen_balance_percent:+.2f}%, which this build classifies "
        f"as {result.band!r} — but it is quoted in the {band!r} interpretation, so a reader is "
        "told it is an example of a band it is not in"
    )
    assert result.oxygen_balance_percent == pytest.approx(quoted, abs=tolerance), (
        f"the {band!r} interpretation quotes {formula} at {token}, and this build computes "
        f"{result.oxygen_balance_percent:+.2f}%"
    )
    assert token in result.interpretation, (
        f"{token} is no longer the spelling the {band!r} interpretation uses, so this table and "
        f"the string it checks have come apart: {result.interpretation!r}"
    )


def test_every_band_is_bounded_above_and_an_oxidiser_is_not_called_near_balanced() -> None:
    """Every band is bounded above, so a strong oxidiser is not called near-balanced.

    Asserted structurally (each band's ceiling is the next band's floor, every interpretation names
    a compound) rather than with spot values, so an added band cannot reopen the hole at its own
    top.
    """
    floors = [floor for floor, _, _ in oxygen_balance_module._BAND_FLOORS]
    assert floors == sorted(floors, reverse=True), (
        f"the band floors are matched top-down by the first `percent >= floor`, so they must "
        f"descend: {floors}"
    )
    assert floors[0] > 0.0, (
        "the highest band floor is not positive, so a compound carrying surplus oxygen shares a "
        f"band with a balanced one: {floors}"
    )

    named = {band for _, _, band in QUOTED_IN_INTERPRETATIONS}
    declared = {name for _, name, _ in oxygen_balance_module._BAND_FLOORS}
    declared.add(oxygen_balance_module._VERY_DEFICIENT[0])
    assert named == declared, (
        f"a band nobody quotes a compound in is a band nothing recomputes: {declared - named} have "
        f"no example, {named - declared} are quoted for a band that no longer exists"
    )

    for formula in ("KClO4", "H2O2", "O2"):
        result = oxygen_balance(formula)
        assert result.band != "near-balanced", (
            f"{formula} at {result.oxygen_balance_percent:+.2f}% is classified {result.band!r}; an "
            "oxidiser well above the +40 boundary must not be read as balanced"
        )
        assert "nitroglycerine" not in result.interpretation, (
            f"{formula} is returned with the interpretation that names nitroglycerine and ammonium "
            "nitrate as the compounds sharing its band"
        )
