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

**No RDKit.** OB% needs element counts rather than a molecular graph — so the input is a molecular
*formula* and this module parses one. That is a real narrowing and the tool says so: a SMILES is not
accepted, because accepting one would mean either a silent wrong answer or a 500 MB image for a
division.

**The weights and the tokenizer are `molmass`'s; the refusals are this module's, and that split is
the whole design.** `molmass` is BSD-3 with **zero** required dependencies — the only reason it is
admissible in the shortest closure in this fleet — and it carries the standard atomic weights and a
formula grammar that were previously seventeen hand-transcribed floats and a regex here. What it is
*not* allowed to decide is what this server will answer about: it parses `Ca(NO3)2` and
`CuSO4.5H2O` happily, and it reads `2H2O` as **deuterium oxide** rather than as two waters
(measured against molmass 2026.1.8). Each of those is a notation a chemist writes and this screen
would get silently wrong — `Ca(NO3)2` read token-wise is wrong by a factor of two on the element
that decides the whole number — so every refusal `parse_formula` made before still runs, and it
runs **in front of** the library rather than being delegated to it. The seventeen-element allowlist
stays for the same reason: it is a "refuse rather than approximate" policy about what this screen
has been reviewed for, not a limit on what a parser could manage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from molmass import ELEMENTS, Formula
from molmass import FormulaError as MolmassFormulaError

#: The elements a process chemist's energetic candidates are actually built from. Deliberately
#: *not* a full periodic table: an element outside this set is refused by name rather than
#: approximated, which is this fleet's "refuse rather than approximate" rule applied to a screen's
#: reviewed domain. It is a policy and not an implementation limit — `molmass` knows all 109 — so
#: adding one is a one-line change in a reviewed commit, which is the right cost for an element
#: whose oxygen demand nobody here has decided a convention for.
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

#: Standard atomic weights, g/mol, for exactly those elements — read from `molmass` rather than
#: transcribed. It stays a module-level `dict` because it is this server's vendored corpus and
#: `engine/selftest.py` is the checksum it does not otherwise have: the readiness probe breaks an
#: entry and reads the status, which is what proves the probe runs the arithmetic rather than
#: importing it.
#:
#: Replacing the transcription moved every weight a little and no answer measurably. Measured over
#: the 22 formulas in `tests/test_oxygen_balance.py` and this module's own published cases, the
#: largest molar-mass change is **+0.0084 g/mol** (NCl3, from chlorine's 35.45 → 35.4529) and the
#: largest OB% change is **-0.0123 percentage points** (methane). No compound crossed a band floor;
#: the closest any came to one was several points away. Both figures are dated measurements of the
#: commit that made the change, which is why they are here and not in a test — the test that keeps
#: it true is the published-value comparison, at a tolerance forty times the shift.
ATOMIC_WEIGHTS: dict[str, float] = {
    symbol: float(ELEMENTS[symbol].mass) for symbol in sorted(ALLOWED_ELEMENTS)
}

#: The classification bands. Sub-ranges of OB% and what each one licenses a reader to conclude —
#: which in every band is "what to do next", never "this is safe" or "this is an explosive".
#: The -200/-80/-40/+40 boundaries are the conventional screening bands (Bretherick's *Handbook of
#: Reactive Chemical Hazards*, 8th ed., §2.3.3); they are a triage convention rather than a physical
#: threshold, and the interpretation strings say so.
#:
#: **All four boundaries are floors here, and the +40 one was missing.** This tuple held three, so
#: the top band had no upper bound and everything above -40 landed in it — measured, `KClO4`
#: (+40.42%), `H2O2` (+47.04%) and even `O2` (+100.00%) were returned as *near-balanced* under an
#: interpretation reading "this is the range nitroglycerine (+3.5%) and ammonium nitrate (+20%) sit
#: in". A strongly oxygen-rich oxidiser is a different hazard with a different action — the question
#: is what it will do to a fuel it is mixed with or contaminated by, not what it does alone — and
#: the convention's fourth boundary exists to say so.
#:
#: **Every percentage quoted in an interpretation is this module's own arithmetic**, and one was
#: not: the `oxygen-deficient` band cited "toluene is -302%" where this module computes -312.57%,
#: at which toluene is in the band *below* the one citing it. `tests/test_oxygen_balance.py`
#: recomputes every
#: cited figure from its formula, so a quoted number cannot drift from the code again.
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


#: One element symbol and its optional count. A formula this screen answers is nothing but these,
#: back to back, so a string that is not a run of them is refused before `molmass` sees it.
_ELEMENT_TOKEN = re.compile(r"([A-Z][a-z]?)(\d*)")
_PLAIN_FORMULA = re.compile(r"(?:[A-Z][a-z]?\d*)+")

#: Acronyms this screen's domain meets that are spelled entirely from real element symbols, so no
#: rule over the string can tell them from a formula: `BPO` parses as one boron, one phosphorus and
#: one oxygen and gets an OB% for that. Refused by name, with what the acronym stands for, because
#: the symbol check below only catches an acronym with a letter that is not an element (`TNT`,
#: `PETN`, `DMSO`). A list, and therefore not closed: any other all-element acronym still parses,
#: and the composition and molar mass returned beside the balance are how a caller catches it.
_ACRONYMS_THAT_SPELL_A_FORMULA: dict[str, str] = {
    "BPO": "benzoyl peroxide, C14H10O4",
    "CHP": "cumene hydroperoxide, C9H12O2",
    "NC": "nitrocellulose, a polymer — write the repeat unit, e.g. C6H7N3O11 for the trinitrate",
    "NaN": "a missing number rather than a compound",
}


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

    Deliberately narrow: no nesting, no grouping of any kind, no hydrates, no charges, no isotopes,
    no leading multiplier. Each of those is a thing a chemist writes, `molmass` parses, and this
    screen would report a wrong number for — `Ca(NO3)2` expanded correctly is one calcium, two
    nitrogens and **six** oxygens, so a caller who meant the salt and a parser that read three
    oxygens disagree by a factor of two on the element that decides the whole answer. Delegating the
    notation to the library would be exactly that: `molmass` answers `Ca(NO3)2`, and it answers
    `2H2O` as deuterium oxide. So each notation is refused by name, before the library sees the
    string, and the message says what to write instead.

    **All three bracket pairs, because a refusal set with a hole in it is not a policy**
    (`D-2026-09-16-a-refusal-set-with-a-hole-in-it-is-not-a-refusal-set`). The blocklist held
    `()` and `[]` and not `{}`, so `{H2O}2` came back as `{'H': 4.0, 'O': 2.0}` — molmass expands a
    brace group like any other — while `(H2O)2` and `[H2O]2` were refused by name. The *number* was
    right, which is what makes it worth fixing rather than shrugging at: one notation silently
    delegated to the library is one notation nobody reviewed, and the next brace group is a salt
    rather than a hydrate. The pre-`molmass` parser refused braces because its regex accepted
    nothing but element symbols and digits; the blocklist that replaced it enumerated, and
    enumerations drop things.

    **Two things this parser accepts that its predecessor did not, kept deliberately and recorded
    here rather than left to be rediscovered.** Whitespace inside a formula (`"C6 H5 NO2"`) now
    parses, because `molmass` ignores it — the answer is the one the chemist meant, and refusing a
    copy-pasted formula for its spaces would be pedantry rather than safety. And an explicit zero
    count (`"C0"`, `"C1H0"`) is now *refused* where the old parser returned a zero, which is the
    stricter direction: an element written with a count of nothing is a typo, not a composition.

    The weights are the library's; the domain is this module's. An element outside
    `ALLOWED_ELEMENTS` is named in the refusal rather than dropped, because a silently ignored
    element returns a molar mass that is too low and therefore an OB% that is too *favourable*.

    **Every symbol is checked against that set before `molmass` runs, not only after.** `molmass`
    expands abbreviations and residue codes silently, so a compound name or acronym typed where a
    formula belongs came back as a confident, wrong answer in the reassuring direction: `PETN`
    parsed as C18H29N5O9 at -144.5% (real PETN is about -10%), `TNT` as C12H22N4O7 at -134% against
    the real -74%, `THF` as C19H25N5O5, `Et2O` as C4H10O. So the string must be a run of element
    symbols and counts, and each symbol must be in the table — `E`, `T`, `D`, `Me`, `Et` are named
    and refused. The post-parse check stays as the backstop for anything the library still invents.

    **That catches an acronym only when one of its letters is not an element, and it said more.**
    `BPO` (benzoyl peroxide) reads as {B, P, O}, `CHP` (cumene hydroperoxide) as {C, H, P}, `NC`
    (nitrocellulose) as {C, N} and `NaN` as {N, Na}: every symbol is real, so no rule over the
    string separates them from a formula without also refusing `HCN` or `COS`. The ones a process-
    safety screen meets are refused by name from `_ACRONYMS_THAT_SPELL_A_FORMULA`; any other
    all-element acronym still parses, and the composition and molar mass returned with the balance
    are what let a caller see the misread.

    Raises:
        FormulaError: the string is empty, is not a run of element symbols and counts, names a
            symbol outside `ALLOWED_ELEMENTS` (an abbreviation or acronym with a non-element letter
            included), is one of the listed all-element acronyms, carries an
            element count of zero, or uses a notation (brackets or braces of any kind, a hydrate
            dot, a charge, a leading multiplier) this parser refuses rather than guesses at.
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
                f"{formula!r} uses {what}, which this parser refuses rather than guesses at — "
                f"{instead}"
            )
    if text[0].isdigit():
        # Measured against molmass 2026.1.8: `2H2O` is parsed as deuterium oxide, not as two
        # waters. A chemist writing a stoichiometric coefficient means the second.
        raise FormulaError(
            f"{formula!r} starts with a count, which this parser refuses rather than guesses at — "
            "a leading number reads as an isotope mass number rather than as a multiplier; write "
            "the whole composition, e.g. 2H2O as H4O2"
        )

    compact = "".join(text.split())
    if compact in _ACRONYMS_THAT_SPELL_A_FORMULA:
        raise FormulaError(
            f"{formula!r} is an acronym ({_ACRONYMS_THAT_SPELL_A_FORMULA[compact]}), not a "
            "molecular formula, though every letter in it is an element symbol; write the "
            "formula itself"
        )
    if not _PLAIN_FORMULA.fullmatch(compact):
        raise FormulaError(
            f"{formula!r} is not a molecular formula; expected element symbols and counts such as "
            "C7H5N3O6"
        )
    for symbol, _count in _ELEMENT_TOKEN.findall(compact):
        if symbol not in ALLOWED_ELEMENTS:
            raise FormulaError(
                f"{formula!r} names {symbol!r}, which is not an element in this server's "
                f"atomic-weight table; it holds {', '.join(sorted(ALLOWED_ELEMENTS))} — a compound "
                "name, an acronym or a group abbreviation (Me, Et, Ph, Ts) is not a formula"
            )

    try:
        composition = Formula(text).composition()
    except MolmassFormulaError as error:
        # `molmass` reports the offending character with a caret on its own lines; the first line
        # is the sentence, and the rest is a pointer at a string the caller already has.
        reason = str(error).splitlines()[0]
        raise FormulaError(
            f"{formula!r} is not a molecular formula ({reason}); expected element symbols and "
            "counts such as C7H5N3O6"
        ) from error

    counts: dict[str, float] = {}
    for symbol in composition:
        if symbol not in ALLOWED_ELEMENTS:
            raise FormulaError(
                f"{formula!r} names element {symbol!r}, which is not in this server's "
                f"atomic-weight table; it holds {', '.join(sorted(ALLOWED_ELEMENTS))}"
            )
        counts[symbol] = float(composition[symbol].count)
    if not counts:
        raise FormulaError(f"{formula!r} names no elements")
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
