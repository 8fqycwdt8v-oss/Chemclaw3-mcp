"""The adiabatic arithmetic, checked against numbers written independently of the code.

Every assertion here is either a hand-computed value, a textbook case, or an *invariant* that must
hold whatever the formula — because a test that re-derives the expression it is testing agrees with
itself forever. Where a literal appears it was computed on paper from the units in the docstring,
which is the only way a unit error is catchable by a test at all.

No transport is imported: `engine/` is pure computation, and this file proves it stays that way by
being runnable with FastAPI uninstalled.
"""

from __future__ import annotations

import math

import pytest
from chemclaw_mcp_thermalsafety.engine.runaway import (
    ThermalInputError,
    adiabatic_temperature_rise,
    heat_removal_capacity,
    mtsr,
    stoessel_class,
    temperature_for_tmr,
    time_to_maximum_rate_hours,
)


def test_the_adiabatic_rise_is_the_hand_computed_one() -> None:
    """150 kJ/mol over 10 mol into 50 kg at 1.9 kJ/(kg·K) is 1500/95 = 15.789 K.

    Written out because the whole value of this function is that the units cancel correctly, and a
    literal computed on paper is the only assertion that can catch a factor of 1000.
    """
    assert adiabatic_temperature_rise(
        heat_of_reaction_kj_per_mol=-150.0,
        moles=10.0,
        mass_kg=50.0,
        specific_heat_kj_per_kg_k=1.9,
    ) == pytest.approx(1500.0 / 95.0)


def test_the_enthalpy_sign_convention_does_not_change_the_rise() -> None:
    """A thermodynamic -150 and a calorimetric +150 are the same exotherm.

    Both conventions reach this function from real reports, and silently disagreeing on them would
    make one of the two return a negative adiabatic rise — which then propagates into an MTSR
    *below* the process temperature and a criticality class of 1 for a dangerous process.
    """
    negative = adiabatic_temperature_rise(
        heat_of_reaction_kj_per_mol=-150.0, moles=10.0, mass_kg=50.0, specific_heat_kj_per_kg_k=1.9
    )
    positive = adiabatic_temperature_rise(
        heat_of_reaction_kj_per_mol=150.0, moles=10.0, mass_kg=50.0, specific_heat_kj_per_kg_k=1.9
    )
    assert negative == positive


def test_a_zero_or_negative_mass_or_heat_capacity_is_refused_by_name() -> None:
    """Each would divide by zero or return a negative rise; the message names which one."""
    for kwargs, expected in (
        ({"mass_kg": 0.0}, "mass_kg"),
        ({"specific_heat_kj_per_kg_k": -1.9}, "specific_heat_kj_per_kg_k"),
        ({"moles": -1.0}, "moles"),
    ):
        base = {
            "heat_of_reaction_kj_per_mol": -150.0,
            "moles": 10.0,
            "mass_kg": 50.0,
            "specific_heat_kj_per_kg_k": 1.9,
        }
        base.update(kwargs)
        with pytest.raises(ThermalInputError, match=expected):
            adiabatic_temperature_rise(**base)


def test_mtsr_is_the_process_temperature_plus_the_accumulated_share() -> None:
    """T_p + X_ac·ΔT_ad, and the two endpoints of the accumulation fraction.

    The endpoints are the assertion that matters: at 0 the reagent is consumed as it arrives and a
    cooling failure changes nothing, at 1 the whole exotherm is in front of you. A formula that got
    the fraction the wrong way round passes a mid-range spot check and fails both of these.
    """
    assert mtsr(
        process_temperature_c=20.0, adiabatic_temperature_rise_k=100.0, accumulation_fraction=0.3
    ) == pytest.approx(50.0)
    assert mtsr(
        process_temperature_c=20.0, adiabatic_temperature_rise_k=100.0, accumulation_fraction=0.0
    ) == pytest.approx(20.0)
    assert mtsr(
        process_temperature_c=20.0, adiabatic_temperature_rise_k=100.0, accumulation_fraction=1.0
    ) == pytest.approx(120.0)


def test_an_accumulation_fraction_outside_zero_to_one_is_refused() -> None:
    """A percentage entered as 30 instead of 0.3 is the realistic mistake, and it is caught."""
    for bad in (30.0, -0.1, 1.5):
        with pytest.raises(ThermalInputError, match="accumulation_fraction"):
            mtsr(
                process_temperature_c=20.0,
                adiabatic_temperature_rise_k=100.0,
                accumulation_fraction=bad,
            )


def test_tmr_falls_as_temperature_rises_and_is_the_hand_computed_value() -> None:
    """The Townsend-Tou expression at one point, plus the monotonicity every use of it assumes.

    TMR_ad = c_p·R·T²/(q·E_a). At 150 °C (423.15 K) with c_p = 1.9 kJ/(kg·K), q = 5 W/kg and
    E_a = 120 kJ/mol: 1900 · 8.314462618 · 423.15² / (5 · 120000) = 4712.8 s = 1.309 h.
    """
    hours = time_to_maximum_rate_hours(
        temperature_c=150.0,
        heat_release_rate_w_per_kg=5.0,
        activation_energy_kj_per_mol=120.0,
        specific_heat_kj_per_kg_k=1.9,
    )
    expected = 1900.0 * 8.314462618 * 423.15**2 / (5.0 * 120000.0) / 3600.0
    assert hours == pytest.approx(expected, rel=1e-9)
    assert hours == pytest.approx(1.309, abs=0.01)


def test_the_temperature_for_a_target_tmr_inverts_the_forward_calculation() -> None:
    """The one property a bisection must have: round-tripping through the forward formula.

    `temperature_for_tmr` extrapolates the rate along Arrhenius and solves; feeding its answer back
    through `time_to_maximum_rate_hours` with the rate extrapolated the same way must return the
    target. Asserted as a round trip rather than against a literal, because a literal would pin the
    bisection's tolerance instead of its correctness.
    """
    reference_c, reference_rate, energy, heat_capacity = 200.0, 50.0, 120.0, 1.9
    t_d24 = temperature_for_tmr(
        target_hours=24.0,
        reference_temperature_c=reference_c,
        heat_release_rate_w_per_kg=reference_rate,
        activation_energy_kj_per_mol=energy,
        specific_heat_kj_per_kg_k=heat_capacity,
    )
    kelvin, reference_kelvin = t_d24 + 273.15, reference_c + 273.15
    rate_there = reference_rate * math.exp(
        -(energy * 1000.0) / 8.314462618 * (1.0 / kelvin - 1.0 / reference_kelvin)
    )
    assert time_to_maximum_rate_hours(
        temperature_c=t_d24,
        heat_release_rate_w_per_kg=rate_there,
        activation_energy_kj_per_mol=energy,
        specific_heat_kj_per_kg_k=heat_capacity,
    ) == pytest.approx(24.0, rel=1e-6)
    assert t_d24 < reference_c, "T_D24 must be below the temperature the fast rate was measured at"


def test_a_target_no_temperature_reaches_is_reported_rather_than_clamped() -> None:
    """The bracket's ends are a finding, not a failure, and the message says which end.

    A clamped -99 °C returned as a T_D24 would go straight into `stoessel_criticality_class` and
    produce a class 5 for a reason that is not physical. Both directions are exercised because they
    mean opposite things: a decomposition fast everywhere, and one too slow to reach the target.
    """
    with pytest.raises(ThermalInputError, match="already below"):
        temperature_for_tmr(
            target_hours=24.0,
            reference_temperature_c=-50.0,
            heat_release_rate_w_per_kg=1e6,
            activation_energy_kj_per_mol=50.0,
            specific_heat_kj_per_kg_k=1.9,
        )
    with pytest.raises(ThermalInputError, match="still above"):
        temperature_for_tmr(
            target_hours=1e-9,
            reference_temperature_c=25.0,
            heat_release_rate_w_per_kg=1e-12,
            activation_energy_kj_per_mol=200.0,
            specific_heat_kj_per_kg_k=1.9,
        )


def test_every_stoessel_ordering_produces_its_documented_class() -> None:
    """All five classes, each from the ordering that defines it.

    Driven as a table because the classification *is* a table: the risk is an off-by-one in the
    comparison chain, which any single case passes and the full set does not. Each row is the
    canonical ordering from Stoessel's classification, written here as four temperatures.
    """
    cases = (
        # (T_p, MTSR, MTT, T_D24) -> class
        ((20.0, 60.0, 100.0, 140.0), 1),  # T_p < MTSR < MTT < T_D24
        ((20.0, 60.0, 140.0, 100.0), 2),  # T_p < MTSR < T_D24 < MTT  (MTT above T_D24)
        ((20.0, 120.0, 100.0, 140.0), 3),  # MTSR > MTT, still below T_D24: boiling is the barrier
        ((20.0, 150.0, 100.0, 140.0), 4),  # MTSR > T_D24, MTT below T_D24
        ((20.0, 150.0, 160.0, 140.0), 5),  # MTSR > T_D24 with MTT above it: no barrier first
    )
    for (process, maximum, technical, decomposition), expected in cases:
        result = stoessel_class(
            process_temperature_c=process,
            mtsr_c=maximum,
            max_technical_temperature_c=technical,
            decomposition_t_d24_c=decomposition,
        )
        assert result.criticality_class == expected, (
            f"T_p={process}, MTSR={maximum}, MTT={technical}, T_D24={decomposition} classified "
            f"{result.criticality_class}, expected {expected}"
        )
        assert result.interpretation, "a class with no interpretation is a number with no meaning"


def test_the_ordering_returned_is_the_evidence_for_the_class() -> None:
    """The four temperatures come back sorted, so a reader can check the class rather than trust it.

    This is the half that makes a classification auditable: a chemist who disagrees with the class
    can see which comparison produced it. Asserted as *sortedness* and completeness rather than
    against a literal tuple, so renaming a label does not fail a test about ordering.
    """
    result = stoessel_class(
        process_temperature_c=20.0,
        mtsr_c=150.0,
        max_technical_temperature_c=100.0,
        decomposition_t_d24_c=140.0,
    )
    values = [value for _, value in result.ordering]
    assert values == sorted(values)
    assert {name for name, _ in result.ordering} == {"T_p", "MTSR", "MTT", "T_D24"}


def test_heat_removal_keeps_its_sign_when_the_jacket_is_warmer() -> None:
    """A jacket above the reactor is a heat source, and an absolute value would hide it.

    The state is real — a batch being brought up to temperature is in it — and reporting it as a
    positive removal would tell a chemist the cooling is working while it is heating.
    """
    cooling_w, cooling_specific = heat_removal_capacity(
        heat_transfer_coefficient_w_per_m2_k=300.0,
        heat_exchange_area_m2=2.0,
        reactor_temperature_c=80.0,
        jacket_temperature_c=20.0,
        mass_kg=100.0,
    )
    assert cooling_w == pytest.approx(300.0 * 2.0 * 60.0)
    assert cooling_specific == pytest.approx(cooling_w / 100.0)

    heating_w, _ = heat_removal_capacity(
        heat_transfer_coefficient_w_per_m2_k=300.0,
        heat_exchange_area_m2=2.0,
        reactor_temperature_c=20.0,
        jacket_temperature_c=80.0,
        mass_kg=100.0,
    )
    assert heating_w < 0


def test_a_temperature_below_absolute_zero_is_refused_everywhere_it_is_taken() -> None:
    """A transposed sign (-20 read as -200) is a typo, and every formula here divides by kelvin.

    Checked across three entry points rather than one, because `_kelvin` guarding only the function
    somebody happened to test is exactly the gap this repository calls a vacuous guard.
    """
    with pytest.raises(ThermalInputError, match="absolute zero"):
        mtsr(
            process_temperature_c=-300.0,
            adiabatic_temperature_rise_k=100.0,
            accumulation_fraction=0.3,
        )
    with pytest.raises(ThermalInputError, match="absolute zero"):
        time_to_maximum_rate_hours(
            temperature_c=-300.0,
            heat_release_rate_w_per_kg=5.0,
            activation_energy_kj_per_mol=120.0,
            specific_heat_kj_per_kg_k=1.9,
        )
    with pytest.raises(ThermalInputError, match="absolute zero"):
        heat_removal_capacity(
            heat_transfer_coefficient_w_per_m2_k=300.0,
            heat_exchange_area_m2=2.0,
            reactor_temperature_c=-300.0,
            jacket_temperature_c=20.0,
            mass_kg=100.0,
        )


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
@pytest.mark.parametrize(
    "argument",
    ["heat_of_reaction_kj_per_mol", "moles", "mass_kg", "specific_heat_kj_per_kg_k"],
)
def test_a_non_finite_input_to_the_adiabatic_rise_is_refused(argument: str, bad: float) -> None:
    """NaN and infinity used to come back as a NaN or infinite rise — `null` over JSON.

    Measured before the guard: `moles=NaN` answered NaN and `moles=Infinity` answered infinity,
    because `value <= 0` is False for both. A safety number is a refusal or a number.
    """
    arguments = {
        "heat_of_reaction_kj_per_mol": -150.0,
        "moles": 10.0,
        "mass_kg": 50.0,
        "specific_heat_kj_per_kg_k": 1.9,
    }
    arguments[argument] = bad
    with pytest.raises(ThermalInputError, match="finite"):
        adiabatic_temperature_rise(**arguments)


@pytest.mark.parametrize("bad", [math.nan, math.inf])
def test_a_non_finite_temperature_or_rise_is_refused_rather_than_carried_into_mtsr(
    bad: float,
) -> None:
    """`nan <= -273.15` is False, so a NaN process temperature passed the absolute-zero check."""
    with pytest.raises(ThermalInputError, match="finite"):
        mtsr(
            process_temperature_c=bad, adiabatic_temperature_rise_k=10.0, accumulation_fraction=0.5
        )
    with pytest.raises(ThermalInputError, match="finite"):
        mtsr(
            process_temperature_c=20.0, adiabatic_temperature_rise_k=bad, accumulation_fraction=0.5
        )
