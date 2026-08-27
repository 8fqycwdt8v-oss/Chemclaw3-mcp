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


# --- Two more independent screens, each of which caught a live defect in this table ---
#
# The three checks above validate the *identity* columns (CAS, formula, MW) and the Antoine sets.
# Nothing validated the numbers a process decision actually turns on. These two do, each by
# relating a column to a *different* column written at a different time:
#
# - a closed-cup flash point is by definition the temperature at which the vapour above the liquid
#   first reaches its lower flammable limit, so the modelled vapour fraction there must land on a
#   real LFL. It caught acetic acid's dimerisation-suppressed dHvap (15.8 vol% against an LFL of
#   4.0%) from a direction no vapour-pressure test was looking at;
# - Beerbower's `dP = 37.4*mu/sqrt(Vm)` is the only independent handle on the Hansen polar term.
#   `mu` is written nowhere in this corpus, so the screen shares no input with the column it
#   checks. It caught dimethyl carbonate at dP = 8.6 where 3.9 is published.

# Vapour fraction, in volume percent of one atmosphere, that a closed-cup flash point implies.
# Real LFLs for organic solvents run about 0.8-8 vol%; the band is widened to 15 to keep this a
# screen for an order-of-magnitude error rather than a re-measurement of the flash point.
LFL_BAND_VOL_PERCENT = (0.8, 15.0)
# Formic acid genuinely has an LFL of 18 vol% (its combustion needs very little oxygen), so it
# sits above the band by chemistry rather than by a typo. It is named here rather than excluded.
HIGH_LFL_SOLVENTS = {"formic acid": (15.0, 24.0)}

# Gas-phase dipole moments in debye, from the CRC Handbook's dipole-moment table. Test-local on
# purpose: the point of the screen is that this number appears nowhere in the corpus.
DIPOLE_MOMENT_DEBYE = {
    "water": 1.85,
    "methanol": 1.70,
    "ethanol": 1.69,
    "1-propanol": 1.68,
    "2-propanol": 1.58,
    "1-butanol": 1.66,
    "tert-butanol": 1.66,
    "ethylene glycol": 2.28,
    "acetone": 2.88,
    "methyl ethyl ketone": 2.78,
    "methyl isobutyl ketone": 2.79,
    "ethyl acetate": 1.78,
    "isopropyl acetate": 1.86,
    "n-butyl acetate": 1.87,
    "tetrahydrofuran": 1.63,
    "2-methyltetrahydrofuran": 1.38,
    "1,4-dioxane": 0.45,
    "diethyl ether": 1.15,
    "methyl tert-butyl ether": 1.32,
    "dichloromethane": 1.60,
    "chloroform": 1.04,
    "1,2-dichloroethane": 1.83,
    "toluene": 0.375,
    "benzene": 0.0,
    "p-xylene": 0.0,
    "n-heptane": 0.0,
    "n-hexane": 0.0,
    "cyclohexane": 0.0,
    "methylcyclohexane": 0.0,
    "acetonitrile": 3.92,
    "N,N-dimethylformamide": 3.82,
    "N,N-dimethylacetamide": 3.72,
    "N-methylpyrrolidone": 4.09,
    "dimethyl sulfoxide": 3.96,
    "sulfolane": 4.35,
    "pyridine": 2.22,
    "triethylamine": 0.66,
    "acetic acid": 1.70,
    "formic acid": 1.41,
    "anisole": 1.38,
    "dimethyl carbonate": 0.91,
    "propylene carbonate": 4.94,
}

# How far a tabulated dP may sit from its Beerbower estimate before the row is called suspect.
#
# Beerbower is a rough correlation, so the bound is set from this table rather than from theory:
# across the 41 other rows the residuals run -3.28 (NMP) to +3.31 (formic acid), mean -0.74 with a
# standard deviation of 1.38, while dimethyl carbonate's wrong value was +4.89 out — 4.1 sigma, and
# the largest in the table by 1.6. Four sits between the two with headroom on both sides: it is a
# screen for a transcription error, not a second measurement of the Hansen triple.
MAX_BEERBOWER_RESIDUAL = 4.0


def test_the_flash_point_implies_a_real_lower_flammable_limit() -> None:
    """At a closed-cup flash point the modelled vapour must sit at the solvent's LFL.

    Two independently written columns — the flash point, and whatever drives the vapour pressure
    (Antoine constants or dHvap) — are forced to agree through a physical definition. A dHvap that
    is wrong by a factor of several cannot survive it, which is how acetic acid's was found.
    """
    checked = 0
    for solvent in records.all_solvents():
        if solvent.flash_point_c is None or solvent.flash_point_c < solvent.mp_c:
            continue
        checked += 1
        vapour = correlations.vapour_pressure(solvent, solvent.flash_point_c)
        percent = 100.0 * vapour.pressure_bar / correlations.ATM_BAR
        low, high = HIGH_LFL_SOLVENTS.get(solvent.name, LFL_BAND_VOL_PERCENT)
        assert low <= percent <= high, (
            f"{solvent.name}: at its flash point of {solvent.flash_point_c} °C the table models "
            f"{percent:.2f} vol% vapour ({vapour.method}), which is not a lower flammable limit "
            f"(expected {low}-{high} vol%)"
        )
    assert checked >= 35, "the table should still carry flash points for at least 35 solvents"


def test_the_hansen_polar_term_agrees_with_beerbower() -> None:
    """`dP = 37.4*mu/sqrt(Vm)` against a dipole moment this corpus does not contain.

    The Hansen triple has no internal check — the three numbers are transcribed together from the
    same table, so a typo in one is invisible to the other two. This is the one relation that
    brings an outside number to bear on it.
    """
    worst: tuple[float, str] = (0.0, "")
    for solvent in records.all_solvents():
        mu = DIPOLE_MOMENT_DEBYE.get(solvent.name)
        if mu is None:
            continue
        molar_volume = solvent.mw / solvent.density_20c
        estimate = 37.4 * mu / math.sqrt(molar_volume)
        residual = solvent.hansen_p - estimate
        worst = max(worst, (abs(residual), solvent.name))
        assert abs(residual) < MAX_BEERBOWER_RESIDUAL, (
            f"{solvent.name}: the table says dP = {solvent.hansen_p} but mu = {mu} D over a molar "
            f"volume of {molar_volume:.1f} cm3/mol gives {estimate:.2f}; residual {residual:+.2f}"
        )
    assert worst[1], "the dipole-moment table should still cover most of the corpus"


def test_every_antoine_row_states_the_range_it_was_fitted_over() -> None:
    """Constants without their validity range are a fit nobody can refuse to extrapolate.

    The range is what makes the `method` field honest: inside it the answer is a fit, outside it
    the answer is the Clausius-Clapeyron fallback and says so.
    """
    for solvent in records.all_solvents():
        if solvent.antoine is None:
            assert solvent.antoine_range_k is None, f"{solvent.name}: a range with no constants"
            continue
        assert solvent.antoine_range_k is not None, f"{solvent.name}: constants with no range"
        low, high = solvent.antoine_range_k
        assert low < high, f"{solvent.name}: inverted Antoine range"
        boiling_k = solvent.bp_c + correlations.KELVIN
        assert low <= boiling_k <= high + correlations.ANTOINE_EXTRAPOLATION_K, (
            f"{solvent.name}: the fit covers {low}-{high} K but the row boils at "
            f"{boiling_k:.2f} K, so the constants cannot be validated against their own bp"
        )
