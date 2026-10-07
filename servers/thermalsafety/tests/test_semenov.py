"""The Semenov balance, checked against its own defining condition rather than against literals.

The crossover is where generation equals loss and their slopes are equal; taking the answer back
to both equations is independent of how the root was found.
"""

from __future__ import annotations

import pytest
from chemclaw_mcp_thermalsafety.engine.runaway import ThermalInputError
from chemclaw_mcp_thermalsafety.engine.semenov import (
    heat_generation_w,
    round_up_to_nearest_five,
    semenov_criticality,
)

#: A 25 kg drum of an organic peroxide: 1 W/kg at 100 °C, E_a = 140 kJ/mol, losing heat at
#: 5 W/(m²·K) over 0.5 m². The reference case every test here perturbs.
DRUM = {
    "mass_kg": 25.0,
    "heat_release_rate_w_per_kg": 1.0,
    "reference_temperature_c": 100.0,
    "activation_energy_kj_per_mol": 140.0,
    "heat_transfer_coefficient_w_per_m2_k": 5.0,
    "surface_area_m2": 0.5,
}


def test_the_answer_satisfies_both_equations_that_define_the_crossover() -> None:
    """Q(T_c) = U·A·(T_c - T_a) and dQ/dT = U·A, checked on the returned point.

    The slope is checked by central difference, not the solver's analytic slope. Tolerances derive
    from the solver bracket: `_TOLERANCE_K` of 1e-4 K is about 1.2e-5 relative error in generation
    here, so a tighter bound would pin the bisection rather than the property.
    """
    balance = semenov_criticality(**DRUM)
    conductance = DRUM["heat_transfer_coefficient_w_per_m2_k"] * DRUM["surface_area_m2"]

    assert balance.heat_generation_at_criticality_w == pytest.approx(
        conductance * balance.self_heating_at_criticality_k, rel=1e-4
    ), "the generation at the returned point does not equal the loss there"

    step = 1e-4
    rates = {
        offset: heat_generation_w(
            temperature_c=balance.critical_contents_c + offset,
            mass_kg=DRUM["mass_kg"],
            heat_release_rate_w_per_kg=DRUM["heat_release_rate_w_per_kg"],
            reference_temperature_c=DRUM["reference_temperature_c"],
            activation_energy_kj_per_mol=DRUM["activation_energy_kj_per_mol"],
        )
        for offset in (-step, step)
    }
    slope = (rates[step] - rates[-step]) / (2 * step)
    assert slope == pytest.approx(conductance, rel=1e-5), (
        "the generation curve's slope at the returned point does not match the loss slope, so the "
        "point is not a tangency"
    )


def test_the_self_heating_is_exactly_r_t_squared_over_ea() -> None:
    """The model's whole content in one identity, and the reason a high E_a is unfavourable here.

    Asserted because it is the non-obvious consequence people get backwards: a *higher* activation
    energy shrinks the tolerable self-heat, so the package sits closer to its own crossover.
    """
    balance = semenov_criticality(**DRUM)
    contents_k = balance.critical_contents_c + 273.15
    assert balance.self_heating_at_criticality_k == pytest.approx(
        8.314462618 * contents_k**2 / (DRUM["activation_energy_kj_per_mol"] * 1000.0), rel=1e-9
    )
    assert balance.critical_ambient_c < balance.critical_contents_c

    hotter = semenov_criticality(**{**DRUM, "activation_energy_kj_per_mol": 200.0})
    assert hotter.self_heating_at_criticality_k < balance.self_heating_at_criticality_k


def test_better_cooling_raises_the_critical_ambient_and_more_material_lowers_it() -> None:
    """Better cooling raises the critical ambient and more material lowers it.

    These are the monotonicities a user reasons with, and a sign error would pass a single spot
    value.
    """
    reference = semenov_criticality(**DRUM)
    better_cooled = semenov_criticality(**{**DRUM, "heat_transfer_coefficient_w_per_m2_k": 20.0})
    assert better_cooled.critical_ambient_c > reference.critical_ambient_c

    bigger = semenov_criticality(**{**DRUM, "mass_kg": 200.0})
    assert bigger.critical_ambient_c < reference.critical_ambient_c


def test_a_package_with_no_crossover_in_range_is_told_which_end_it_ran_off() -> None:
    """A package with no crossover in range is told which end of the bracket it ran off.

    A clamped value would be read as a critical ambient, and "self-heats in a freezer" and "no
    crossover below 400 °C" lead to opposite actions.
    """
    # A rate read at a low reference temperature in the wrong unit: the realistic way to reach this
    # branch. A large rate at the 100 °C reference is attenuated by Arrhenius on the way to -40 °C
    # and does not raise.
    with pytest.raises(ThermalInputError, match="no stable ambient"):
        semenov_criticality(
            **{**DRUM, "heat_release_rate_w_per_kg": 1e6, "reference_temperature_c": -40.0}
        )
    with pytest.raises(ThermalInputError, match="no crossover exists"):
        semenov_criticality(**{**DRUM, "heat_release_rate_w_per_kg": 1e-12})


def test_heat_generation_extrapolates_along_arrhenius_in_both_directions() -> None:
    """The rate at the reference point is the rate given, and it falls as temperature falls.

    The identity at the reference catches a reciprocal written the wrong way round, which keeps the
    extrapolation monotone while every derived number is wrong.
    """
    at_reference = heat_generation_w(
        temperature_c=100.0,
        mass_kg=10.0,
        heat_release_rate_w_per_kg=2.0,
        reference_temperature_c=100.0,
        activation_energy_kj_per_mol=120.0,
    )
    assert at_reference == pytest.approx(20.0)

    colder = heat_generation_w(
        temperature_c=60.0,
        mass_kg=10.0,
        heat_release_rate_w_per_kg=2.0,
        reference_temperature_c=100.0,
        activation_energy_kj_per_mol=120.0,
    )
    hotter = heat_generation_w(
        temperature_c=140.0,
        mass_kg=10.0,
        heat_release_rate_w_per_kg=2.0,
        reference_temperature_c=100.0,
        activation_energy_kj_per_mol=120.0,
    )
    assert colder < at_reference < hotter


def test_the_un_rounding_always_rounds_up_including_on_an_exact_multiple() -> None:
    """Up, never to nearest — a 91.2 °C estimate reported as 90 would overstate the margin.

    The exact-multiple case is included because `ceil` on a float that lands exactly on a boundary
    is where a rounding rule usually goes wrong, and here it must stay put rather than jump.
    """
    assert round_up_to_nearest_five(90.17) == 95
    assert round_up_to_nearest_five(91.2) == 95
    assert round_up_to_nearest_five(90.0) == 90
    assert round_up_to_nearest_five(85.000001) == 90


def test_a_non_positive_input_is_refused_by_name_before_any_search_runs() -> None:
    """A zero area or a negative rate would make the bracket meaningless rather than wrong."""
    for field in ("mass_kg", "heat_release_rate_w_per_kg", "surface_area_m2"):
        with pytest.raises(ThermalInputError, match=field):
            semenov_criticality(**{**DRUM, field: 0.0})
