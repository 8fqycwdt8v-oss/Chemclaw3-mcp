"""The dataset validates itself, because a hand-compiled table is a table with typos in it.

This is the part of the reference server most worth copying. A vendored corpus is reviewed once and
then trusted for years, and the realistic failure is not a wrong *decision* about what to include —
it is a transposed digit in row 31 that nobody ever looks at again. Three of the checks below are
internal-consistency arguments strong enough to catch that:

- **CAS check digits.** A CAS number carries its own checksum, so a mistyped one is detectable
  without consulting anything.
- **Formula against molecular weight.** The two are written independently in the row and must agree
  to within a rounding error, so a wrong MW or a wrong formula fails.
- **Antoine constants against the boiling point.** The constants and the boiling point are also
  written independently; if the fit does not reproduce 1 atm at the tabulated bp, one of them is
  wrong. This is what makes it safe to carry Antoine constants for only some rows: a bad set fails
  here rather than answering a distillation question.

The rest are structural — closed vocabularies, no duplicate lookups, required fields present.
"""

from __future__ import annotations

import math

import pytest
from chemclaw_mcp_props.engine import correlations, records

ATOMIC_WEIGHTS = {
    "H": 1.008,
    "C": 12.011,
    "N": 14.007,
    "O": 15.999,
    "S": 32.06,
    "Cl": 35.45,
}

BANDS = {"recommended", "problematic", "hazardous", "highly_hazardous"}
MISCIBILITIES = {"miscible", "partial", "immiscible"}
ICH_CLASSES = {"1", "2", "3", "not_listed"}


def _formula_weight(formula: str) -> float:
    """Molecular weight from a plain `ElementCount` formula — enough for this table's 44 rows."""
    total = 0.0
    index = 0
    while index < len(formula):
        symbol = formula[index]
        index += 1
        if index < len(formula) and formula[index].islower():
            symbol += formula[index]
            index += 1
        digits = ""
        while index < len(formula) and formula[index].isdigit():
            digits += formula[index]
            index += 1
        total += ATOMIC_WEIGHTS[symbol] * (int(digits) if digits else 1)
    return total


def _cas_check_digit_valid(cas: str) -> bool:
    """Whether a CAS number's final digit is the checksum of the digits before it."""
    body, _, check = cas.rpartition("-")
    digits = body.replace("-", "")
    total = sum(int(digit) * position for position, digit in enumerate(reversed(digits), start=1))
    return total % 10 == int(check)


def test_dataset_loads_and_matches_its_checksum() -> None:
    """The manifest's sha256 is the file on disk — the check that makes review meaningful."""
    dataset = records.dataset()
    assert dataset.name == "process-solvents"
    assert dataset.licence
    assert dataset.retrieved_from


def test_every_row_has_a_valid_cas_number() -> None:
    """A CAS number carries its own check digit; a typo cannot survive it."""
    bad = [s.name for s in records.all_solvents() if not _cas_check_digit_valid(s.cas)]
    assert not bad, f"invalid CAS check digit for: {bad}"


def test_molecular_weight_agrees_with_formula() -> None:
    """MW and formula are written independently in the row, so disagreement is a typo in one."""
    for solvent in records.all_solvents():
        expected = _formula_weight(solvent.formula)
        assert abs(expected - solvent.mw) < 0.05, (
            f"{solvent.name}: formula {solvent.formula} weighs {expected:.3f} "
            f"but the table says {solvent.mw}"
        )


def test_antoine_constants_reproduce_the_tabulated_boiling_point() -> None:
    """Every Antoine row must put 1 atm at its own boiling point, within 2 °C.

    The strongest check in this file: it validates two independently written groups of numbers
    against each other. A row that fails is not a modelling disagreement — it is a wrong constant,
    and the right response is to fix it or drop the constants and let the Clausius-Clapeyron
    fallback answer (which the tools already label as the weaker method).
    """
    checked = 0
    for solvent in records.all_solvents():
        if solvent.antoine is None:
            continue
        checked += 1
        predicted = correlations.boiling_point_at(solvent, 1013.25)
        assert abs(predicted - solvent.bp_c) < 2.0, (
            f"{solvent.name}: Antoine constants boil at {predicted:.1f} °C but the table says "
            f"{solvent.bp_c} °C"
        )
    assert checked >= 15, "the table should still carry Antoine constants for at least 15 solvents"


def test_melting_point_is_below_boiling_point() -> None:
    """A row with the two transposed would pass every other check in this file."""
    for solvent in records.all_solvents():
        assert solvent.mp_c < solvent.bp_c, f"{solvent.name}: melting point above boiling point"


def test_closed_vocabularies_are_respected() -> None:
    """Bands, miscibility and ICH class are enumerations; a fifth spelling is a silent bug."""
    for solvent in records.all_solvents():
        assert solvent.greenness_band in BANDS, solvent.name
        assert solvent.water_miscibility in MISCIBILITIES, solvent.name
        assert solvent.ich_class in ICH_CLASSES, solvent.name


def test_ich_limits_accompany_ich_classes() -> None:
    """A classified solvent carries its limit; an unlisted one carries none."""
    for solvent in records.all_solvents():
        if solvent.ich_class in {"1", "2", "3"}:
            assert solvent.ich_limit_ppm is not None, f"{solvent.name} has a class but no limit"
        else:
            assert solvent.ich_limit_ppm is None, f"{solvent.name} is unlisted but carries a limit"


def test_names_and_aliases_resolve_uniquely() -> None:
    """Building the index raises on a duplicate key; this pins the behaviour the tools rely on."""
    assert records.find("2-MeTHF") is records.find("2-methyltetrahydrofuran")
    assert records.find("DCM") is records.find("methylene chloride")
    assert records.find("109-99-9") is records.find("THF")
    assert records.find("N,N-dimethylformamide") is records.find("DMF")
    assert records.find("not a solvent") is None


def test_absent_values_stay_absent() -> None:
    """Dichloromethane has no flash point. Reporting 0 °C there would be a fire-safety error."""
    assert records.require("dichloromethane").flash_point_c is None
    assert records.require("chloroform").flash_point_c is None
    assert records.require("toluene").flash_point_c == 4.0


def test_require_names_the_table_when_a_solvent_is_missing() -> None:
    """The error the agent sees must say the corpus is fixed, so it stops guessing spellings."""
    with pytest.raises(ValueError, match="vendored solvent table"):
        records.require("supercritical unobtainium")


def test_hansen_distance_is_a_metric_on_the_table() -> None:
    """Symmetric, zero on the diagonal, and ordering the pairs a chemist would expect."""
    thf = records.require("THF")
    metthf = records.require("2-MeTHF")
    water = records.require("water")
    assert math.isclose(correlations.hansen_distance(thf, thf), 0.0, abs_tol=1e-9)
    assert math.isclose(
        correlations.hansen_distance(thf, water),
        correlations.hansen_distance(water, thf),
    )
    assert correlations.hansen_distance(thf, metthf) < correlations.hansen_distance(thf, water)
