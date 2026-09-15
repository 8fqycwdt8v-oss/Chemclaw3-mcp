"""Oxygen balance: the one screen here that reads a molecule rather than a calorimetry number.

Oxygen balance to CO2 is the classic first-pass indicator of energetic potential — how far a
compound is from carrying the oxygen it would need to burn itself completely. It is the number a
process chemist wants before committing an unfamiliar nitro, azide or peroxide to scale, precisely
because it can be computed from a formula alone, months before anybody books DSC time.

**What it is not, stated here because the docstring a model reads is one sentence long.** OB% is a
*stoichiometric* quantity. It knows nothing about kinetics, about whether the compound decomposes
at all, or about how much energy is released when it does — TNT sits at -74% and is an explosive;
glucose sits at -107% and is food. A value near zero means "if this decomposes, the stoichiometry
is favourable for a violent one", never "this is an explosive", and a very negative value is not a
clearance. The screen's only correct use is to decide whether the *structural* alerts and the
calorimetry are worth pursuing, which is why the tool returns them together with the number.

**No RDKit.** This server's dependency closure is the MCP transport and nothing else, for the
reason `servers/props/pyproject.toml` gives, and OB% needs element counts rather than a molecular
graph — so the input is a molecular *formula* and this module parses one. That is a real narrowing
and the tool says so: a SMILES is not accepted, because accepting one would mean either a silent
wrong answer or a 500 MB image for a division.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Standard atomic weights (IUPAC 2021 conventional values), g/mol, for the elements a process
#: chemist's energetic candidates are actually built from. Deliberately *not* a full periodic table:
#: an element outside this set is refused by name rather than approximated, which is this fleet's
#: "refuse rather than approximate" rule applied to a table. Adding one is a one-line change in a
#: reviewed commit, which is the right cost for a number that goes into a hazard screen.
ATOMIC_WEIGHTS: dict[str, float] = {
    "H": 1.008,
    "B": 10.81,
    "C": 12.011,
    "N": 14.007,
    "O": 15.999,
    "F": 18.998403162,
    "Na": 22.98976928,
    "Mg": 24.305,
    "Al": 26.9815384,
    "Si": 28.085,
    "P": 30.973761998,
    "S": 32.06,
    "Cl": 35.45,
    "K": 39.0983,
    "Ca": 40.078,
    "Br": 79.904,
    "I": 126.90447,
}

#: The classification bands. Sub-ranges of OB% and what each one licenses a reader to conclude —
#: which in every band is "what to do next", never "this is safe" or "this is an explosive".
#: The -200/-80/-40/+40 boundaries are the conventional screening bands (Bretherick's *Handbook of
#: Reactive Chemical Hazards*, 8th ed., §2.3.3); they are a triage convention rather than a physical
#: threshold, and the interpretation strings say so.
_BAND_FLOORS: tuple[tuple[float, str, str], ...] = (
    (
        -40.0,
        "near-balanced",
        "Within the band where a decomposition, if one occurs, has favourable stoichiometry for a "
        "rapid and complete one. Treat as an energetic candidate until DSC/ARC says otherwise: "
        "this is the range nitroglycerine (+3.5%) and ammonium nitrate (+20%) sit in.",
    ),
    (
        -80.0,
        "moderately oxygen-deficient",
        "Short of the oxygen for complete combustion but not by much — the band most secondary "
        "explosives occupy (TNT is -74%). Not a clearance; a reason to look at the structural "
        "alerts and to measure a decomposition onset before scaling.",
    ),
    (
        -200.0,
        "oxygen-deficient",
        "Far from balanced. Most ordinary organic compounds are here (toluene is -302%, glucose "
        "-107%), so this band carries almost no information on its own and must not be read as a "
        "clearance: an energetic group in the structure still decides the hazard.",
    ),
)

#: What a value below the lowest band floor means.
_VERY_DEFICIENT = (
    "strongly oxygen-deficient",
    "Typical of a hydrocarbon or a simple organic with no oxidiser in it. On this screen alone "
    "there is nothing to pursue — but the screen sees stoichiometry only, so a structural hazard "
    "alert or a known-unstable functional group overrides it entirely.",
)

#: A formula token: an element symbol (one capital, optional lowercase) and an optional count.
_TOKEN = re.compile(r"([A-Z][a-z]?)(\d*)")


class FormulaError(ValueError):
    """A molecular formula this module will not guess at.

    A `ValueError` so the message reaches the model verbatim through `connector_app` — every one of
    them names the offending substring, because "invalid formula" tells a chemist nothing about
    which of `C6H5NO2 · H2O` the parser choked on.
    """


@dataclass(frozen=True)
class OxygenBalance:
    """The screen's whole answer: the number, how it was built, and what it licenses."""

    #: Oxygen balance to CO2, percent by mass. Negative means oxygen-deficient.
    oxygen_balance_percent: float
    #: Molar mass in g/mol, derived from the same element counts — returned so a caller can check
    #: the parse against the value they already know, which is the cheapest possible typo catch.
    molar_mass_g_per_mol: float
    #: The parsed composition, element → count. Returned for the same reason.
    composition: dict[str, float]
    #: One of the band names in `_BAND_FLOORS` or `_VERY_DEFICIENT`.
    band: str
    #: What the band licenses a reader to conclude.
    interpretation: str


def parse_formula(formula: str) -> dict[str, float]:
    """Element counts from a plain molecular formula such as `C6H5NO2` or `C3H5N3O9`.

    Deliberately narrow: no nesting, no parentheses, no hydrates, no charges. Each of those is a
    thing a chemist writes and this parser would get *silently* wrong if it tried — `Ca(NO3)2`
    read token-wise is calcium, nitrogen and three oxygens, an answer that is wrong by a factor
    of two on the element that decides the whole number. So each is refused by name instead, and
    the message says what to write instead.

    Raises:
        FormulaError: the string is empty, holds a character no formula contains, names an element
            outside `ATOMIC_WEIGHTS`, carries no carbon-free interpretation, or uses a notation
            (parentheses, a hydrate dot, a charge) this parser refuses rather than guesses at.
    """
    text = formula.strip()
    if not text:
        raise FormulaError("no molecular formula given")
    for character, what, instead in (
        ("(", "parentheses", "expand the group, e.g. Ca(NO3)2 as CaN2O6"),
        (")", "parentheses", "expand the group, e.g. Ca(NO3)2 as CaN2O6"),
        (".", "a hydrate or salt dot", "write the whole composition, e.g. CuSO4.5H2O as CuSH10O9"),
        ("·", "a hydrate or salt dot", "write the whole composition, e.g. CuSO4·5H2O as CuSH10O9"),
        ("+", "a charge", "oxygen balance is defined for a neutral composition"),
        ("-", "a charge", "oxygen balance is defined for a neutral composition"),
    ):
        if character in text:
            raise FormulaError(
                f"{formula!r} uses {what}, which this parser refuses rather than guesses at — "
                f"{instead}"
            )

    counts: dict[str, float] = {}
    position = 0
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None:
            raise FormulaError(
                f"{formula!r} is not a molecular formula at position {position} "
                f"({text[position]!r}); expected an element symbol such as C, H, N or Cl"
            )
        symbol, digits = match.group(1), match.group(2)
        if symbol not in ATOMIC_WEIGHTS:
            raise FormulaError(
                f"{formula!r} names element {symbol!r}, which is not in this server's "
                f"atomic-weight table; it holds {', '.join(sorted(ATOMIC_WEIGHTS))}"
            )
        counts[symbol] = counts.get(symbol, 0.0) + (float(digits) if digits else 1.0)
        position = match.end()
    return counts


def molar_mass(composition: dict[str, float]) -> float:
    """Molar mass in g/mol from element counts, over the same table the balance uses."""
    return sum(ATOMIC_WEIGHTS[symbol] * count for symbol, count in composition.items())


def oxygen_balance(formula: str) -> OxygenBalance:
    """Oxygen balance to CO2, as a percentage by mass, with its band.

    `OB% = -1600 · (2C + H/2 + 2S - O - <halogen correction>) / M`, the standard form: every carbon
    takes two oxygens to reach CO2, every two hydrogens take one to reach H2O, sulfur takes two to
    reach SO2, and each halogen consumes one hydrogen as HX before that hydrogen can be burned.
    Metals are treated as taking one oxygen per equivalent — the convention for an alkali or
    alkaline-earth counter-ion in an oxidiser salt, which is the case that puts a metal in an
    energetic formula at all.

    Raises:
        FormulaError: whatever `parse_formula` refuses, or a formula whose molar mass is zero.
    """
    composition = parse_formula(formula)
    mass = molar_mass(composition)
    if mass <= 0:
        raise FormulaError(f"{formula!r} has a molar mass of {mass} g/mol")

    carbon = composition.get("C", 0.0)
    hydrogen = composition.get("H", 0.0)
    oxygen = composition.get("O", 0.0)
    sulfur = composition.get("S", 0.0)
    halogens = sum(composition.get(symbol, 0.0) for symbol in ("F", "Cl", "Br", "I"))
    # An alkali/alkaline-earth cation in an oxidiser salt carries its own oxygen demand; the
    # valence is the convention rather than a computed one, which is why only these appear.
    metal_equivalents = (
        composition.get("Na", 0.0)
        + composition.get("K", 0.0)
        + 2.0 * composition.get("Mg", 0.0)
        + 2.0 * composition.get("Ca", 0.0)
        + 3.0 * composition.get("Al", 0.0)
    )
    # Each halogen ties up one hydrogen as HX, so that hydrogen no longer demands oxygen.
    burnable_hydrogen = max(hydrogen - halogens, 0.0)
    demand = (
        2.0 * carbon + burnable_hydrogen / 2.0 + 2.0 * sulfur + metal_equivalents / 2.0 - oxygen
    )
    percent = -1600.0 * demand / mass

    band, interpretation = _VERY_DEFICIENT
    for floor, name, text in _BAND_FLOORS:
        if percent >= floor:
            band, interpretation = name, text
            break
    return OxygenBalance(
        oxygen_balance_percent=percent,
        molar_mass_g_per_mol=mass,
        composition=composition,
        band=band,
        interpretation=interpretation,
    )
