"""Oxygen balance: the one screen here that reads a molecule rather than a calorimetry number.

Oxygen balance to CO2 is the classic first-pass indicator of energetic potential, computable from a
formula before any DSC run. It is stoichiometric only: it knows nothing of kinetics or energy
release (TNT is -74%, glucose -107%). Near zero means a decomposition would be stoichiometrically
favourable for violence, never "this is an explosive"; very negative is not a clearance. Its use is
deciding whether structural alerts and calorimetry are worth pursuing.

The input is a molecular formula, not a SMILES (no RDKit in this closure). Atomic weights and the
tokenizer are `molmass`'s (BSD-3, no dependencies); the refusals are this module's and run *before*
the library, because `molmass` happily parses notations this screen would get silently wrong
(`Ca(NO3)2`, hydrates, `2H2O` as deuterium oxide, abbreviations). `ALLOWED_ELEMENTS` is a
reviewed-domain policy, not a parser limit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mcp_server_kit.limits import echo
from molmass import ELEMENTS, Formula
from molmass import FormulaError as MolmassFormulaError

#: The elements a process chemist's energetic candidates are built from. Anything else is refused by
#: name rather than approximated; adding one is a reviewed one-line change.
ALLOWED_ELEMENTS: frozenset[str] = frozenset(
    {
        "H",
        "B",
        "C",
        "N",
        "O",
        "F",
        "Na",
        "Mg",
        "Al",
        "Si",
        "P",
        "S",
        "Cl",
        "K",
        "Ca",
        "Br",
        "I",
    }
)

#: Standard atomic weights, g/mol, for exactly those elements, read from `molmass`. A module-level
#: `dict` because it is this server's corpus: `engine/selftest.py` recomputes published values from
#: it, and the readiness test breaks an entry to prove the probe runs the arithmetic.
ATOMIC_WEIGHTS: dict[str, float] = {
    symbol: float(ELEMENTS[symbol].mass) for symbol in sorted(ALLOWED_ELEMENTS)
}

#: The classification bands as floors, each with what it licenses a reader to do next — never "safe"
#: or "explosive". The -200/-80/-40/+40 boundaries are the conventional screening bands
#: (Bretherick's *Handbook of Reactive Chemical Hazards*, 8th ed., §2.3.3), a triage convention
#: rather than a physical threshold. Above +40 is an oxidiser, a different hazard. Every percentage
#: quoted in an interpretation is recomputed from its formula by `tests/test_oxygen_balance.py`.
_BAND_FLOORS: tuple[tuple[float, str, str], ...] = (
    (
        40.0,
        "oxygen-rich",
        "Carries more oxygen than it needs to burn itself completely, so on this screen it is an "
        "oxidiser rather than a self-sufficient energetic (KClO4 is +40%, H2O2 +47%). The question "
        "it raises is a different one: what this will do to a fuel it is mixed with, contaminated "
        "by or spilled onto — incompatibility, not self-decomposition. Review the segregation and "
        "the materials of construction, and do not read a positive balance as a clearance.",
    ),
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
        "Far from balanced. Many ordinary organic compounds are here (glucose is -107%, sucrose "
        "-112%), so this band carries almost no information on its own and must not be read as a "
        "clearance: an energetic group in the structure still decides the hazard.",
    ),
)

#: What a value below the lowest band floor means.
_VERY_DEFICIENT = (
    "strongly oxygen-deficient",
    "Typical of a hydrocarbon or a simple organic with no oxidiser in it (toluene is -313%, "
    "methane -399%). On this screen alone there is nothing to pursue — but the screen sees "
    "stoichiometry only, so a structural hazard alert or a known-unstable functional group "
    "overrides it entirely.",
)


#: One element symbol and its optional count; a formula that is not a run of these is refused before
#: `molmass` sees it.
_ELEMENT_TOKEN = re.compile(r"([A-Z][a-z]?)(\d*)")
_PLAIN_FORMULA = re.compile(r"(?:[A-Z][a-z]?\d*)+")

#: Acronyms spelled entirely from element symbols (`BPO` parses as B, P, O), refused by name with
#: what they stand for. Not closed: other such acronyms still parse, and the returned composition
#: and molar mass are how a caller catches them.
_ACRONYMS_THAT_SPELL_A_FORMULA: dict[str, str] = {
    "BPO": "benzoyl peroxide, C14H10O4",
    "CHP": "cumene hydroperoxide, C9H12O2",
    "NC": "nitrocellulose, a polymer — write the repeat unit, e.g. C6H7N3O11 for the trinitrate",
    "NaN": "a missing number rather than a compound",
}


class FormulaError(ValueError):
    """A molecular formula this module will not guess at.

    A `ValueError` so the message, which names the offending substring, reaches the model verbatim.
    """


@dataclass(frozen=True)
class OxygenBalance:
    """The screen's whole answer: the number, how it was built, and what it licenses."""

    #: Oxygen balance to CO2, percent by mass. Negative means oxygen-deficient.
    oxygen_balance_percent: float
    #: Molar mass in g/mol from the same counts, so a caller can check the parse against a known
    #: value.
    molar_mass_g_per_mol: float
    #: The parsed composition, element → count. Returned for the same reason.
    composition: dict[str, float]
    #: One of the band names in `_BAND_FLOORS` or `_VERY_DEFICIENT`.
    band: str
    #: What the band licenses a reader to conclude.
    interpretation: str


def parse_formula(formula: str) -> dict[str, float]:
    """Element counts from a plain molecular formula such as `C6H5NO2` or `C3H5N3O9`.

    Deliberately narrow: no brackets or braces of any kind, hydrates, charges, isotopes or leading
    multipliers. Each is something `molmass` parses and this screen could misreport (`Ca(NO3)2` has
    six oxygens, not three), so each is refused by name before the library runs, saying what to
    write instead. Whitespace is ignored; an explicit zero count is refused as a typo.

    Every symbol must be in `ALLOWED_ELEMENTS` before `molmass` runs, because the library silently
    expands abbreviations (`PETN`, `TNT`, `Et2O`) into wrong formulas whose OB% errs in the
    reassuring direction; a post-parse check is the backstop. Acronyms made only of real symbols are
    refused from `_ACRONYMS_THAT_SPELL_A_FORMULA`.

    Raises:
        FormulaError: The string is empty, is not a run of element symbols and counts, names a
            symbol outside `ALLOWED_ELEMENTS`, is a listed all-element acronym, carries a zero
            count, or uses a refused notation.
    """
    text = formula.strip()
    if not text:
        raise FormulaError("no molecular formula given")
    for character, what, instead in (
        ("(", "parentheses", "expand the group, e.g. Ca(NO3)2 as CaN2O6"),
        (")", "parentheses", "expand the group, e.g. Ca(NO3)2 as CaN2O6"),
        ("[", "isotope or group brackets", "write the natural-abundance composition, e.g. H2O"),
        ("]", "isotope or group brackets", "write the natural-abundance composition, e.g. H2O"),
        ("{", "braces", "expand the group, e.g. {H2O}2 as H4O2"),
        ("}", "braces", "expand the group, e.g. {H2O}2 as H4O2"),
        (".", "a hydrate or salt dot", "write the whole composition, e.g. CuSO4.5H2O as CuSH10O9"),
        ("·", "a hydrate or salt dot", "write the whole composition, e.g. CuSO4·5H2O as CuSH10O9"),
        ("+", "a charge", "oxygen balance is defined for a neutral composition"),
        ("-", "a charge", "oxygen balance is defined for a neutral composition"),
    ):
        if character in text:
            raise FormulaError(
                f"{echo(formula)!r} uses {what}, which this parser refuses rather than guesses "
                "at — "
                f"{instead}"
            )
    if text[0].isdigit():
        # `molmass` reads `2H2O` as deuterium oxide; a chemist means two waters.
        raise FormulaError(
            f"{echo(formula)!r} starts with a count, which this parser refuses rather than "
            "guesses at — "
            "a leading number reads as an isotope mass number rather than as a multiplier; write "
            "the whole composition, e.g. 2H2O as H4O2"
        )

    compact = "".join(text.split())
    if compact in _ACRONYMS_THAT_SPELL_A_FORMULA:
        raise FormulaError(
            f"{echo(formula)!r} is an acronym ({_ACRONYMS_THAT_SPELL_A_FORMULA[compact]}), not a "
            "molecular formula, though every letter in it is an element symbol; write the "
            "formula itself"
        )
    if not _PLAIN_FORMULA.fullmatch(compact):
        raise FormulaError(
            f"{echo(formula)!r} is not a molecular formula; expected element symbols and counts "
            "such as C7H5N3O6"
        )
    for symbol, _count in _ELEMENT_TOKEN.findall(compact):
        if symbol not in ALLOWED_ELEMENTS:
            raise FormulaError(
                f"{echo(formula)!r} names {symbol!r}, which is not an element in this server's "
                f"atomic-weight table; it holds {', '.join(sorted(ALLOWED_ELEMENTS))} — a compound "
                "name, an acronym or a group abbreviation (Me, Et, Ph, Ts) is not a formula"
            )

    try:
        composition = Formula(text).composition()
    except MolmassFormulaError as error:
        # Keep only the first line; the rest is a caret pointer at the caller's own string.
        reason = str(error).splitlines()[0]
        raise FormulaError(
            f"{echo(formula)!r} is not a molecular formula ({reason}); expected element symbols "
            "and "
            "counts such as C7H5N3O6"
        ) from error

    counts: dict[str, float] = {}
    for symbol in composition:
        if symbol not in ALLOWED_ELEMENTS:
            raise FormulaError(
                f"{echo(formula)!r} names element {symbol!r}, which is not in this server's "
                f"atomic-weight table; it holds {', '.join(sorted(ALLOWED_ELEMENTS))}"
            )
        counts[symbol] = float(composition[symbol].count)
    if not counts:
        raise FormulaError(f"{echo(formula)!r} names no elements")
    return counts


def molar_mass(composition: dict[str, float]) -> float:
    """Molar mass in g/mol from element counts, over the same table the balance uses."""
    return sum(ATOMIC_WEIGHTS[symbol] * count for symbol, count in composition.items())


def oxygen_balance(formula: str) -> OxygenBalance:
    """Oxygen balance to CO2, as a percentage by mass, with its band.

    `OB% = -1600 · (2C + max(H - X, 0)/2 + 2S + Meq/2 - O) / M`, X the halogen count and Meq the
    metal equivalents (Na and K count 1, Mg and Ca 2, Al 3): carbon to CO2, hydrogen to H2O,
    sulfur to SO2, and each halogen ties up one hydrogen as HX, so it is subtracted from H inside
    the H/2 term.
    Each metal equivalent is charged half an oxygen (Na2O, CaO, Al2O3), the convention for an
    oxidiser salt's counter-ion.

    Raises:
        FormulaError: Whatever `parse_formula` refuses, or a formula whose molar mass is zero.
    """
    composition = parse_formula(formula)
    mass = molar_mass(composition)
    if mass <= 0:
        raise FormulaError(f"{echo(formula)!r} has a molar mass of {mass} g/mol")

    carbon = composition.get("C", 0.0)
    hydrogen = composition.get("H", 0.0)
    oxygen = composition.get("O", 0.0)
    sulfur = composition.get("S", 0.0)
    halogens = sum(composition.get(symbol, 0.0) for symbol in ("F", "Cl", "Br", "I"))
    # Conventional valences for oxidiser-salt cations, which is why only these appear.
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
