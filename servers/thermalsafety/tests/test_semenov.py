"""The Semenov balance, checked against its own defining condition rather than against literals.

The crossover is *defined* by two equations holding at once — the generation equals the loss, and
their slopes are equal. So the strongest assertion available is to take the answer back to those
two equations and see whether it satisfies them, which is independent of how the root was found.
A test that pinned the returned number would pin the bisection's tolerance instead.
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

    The tangency condition, verified rather than re-derived: the first equation is asserted through
    the returned generation and self-heating, and the second numerically as a central difference,
    which does not reuse the analytic slope the solver itself used.

    **The tolerances are derived from the solver's bracket rather than picked.** `_TOLERANCE_K` is
    1e-4 K, and `dQ/Q = Ea/(R·T²)·dT` turns that into 1.2e-5 of relative error in the generation at
    this case's 371.5 K — so 1e-6 would be asserting the bisection is tighter than it says it is,
    which is how a test ends up pinning an implementation detail instead of a property. Measured
    before this line was written: the identity holds to 1.8e-6, comfortably inside its own bound.
    """
    balance = semenov_criticality(**DRUM)  # type: ignore[arg-type]
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
    balance = semenov_criticality(**DRUM)  # type: ignore[arg-type]
    contents_k = balance.critical_contents_c + 273.15
    assert balance.self_heating_at_criticality_k == pytest.approx(
        8.314462618 * contents_k**2 / (DRUM["activation_energy_kj_per_mol"] * 1000.0), rel=1e-9
    )
    assert balance.critical_ambient_c < balance.critical_contents_c

    hotter = semenov_criticality(**{**DRUM, "activation_energy_kj_per_mol": 200.0})  # type: ignore[arg-type]
    assert hotter.self_heating_at_criticality_k < balance.self_heating_at_criticality_k


def test_better_cooling_raises_the_critical_ambient_and_more_material_lowers_it() -> None:
    """Two monotonicities that any correct implementation has and a sign error does not.

    These are the properties a user reasons with — "a smaller drum is safer to store warmer", "more
    insulation is worse" — so getting one backwards is a defect nobody would catch from a single
    spot value, and both are checked against the same reference case.
    """
    reference = semenov_criticality(**DRUM)  # type: ignore[arg-type]
    better_cooled = semenov_criticality(
        **{**DRUM, "heat_transfer_coefficient_w_per_m2_k": 20.0}  # type: ignore[arg-type]
    )
    assert better_cooled.critical_ambient_c > reference.critical_ambient_c

    bigger = semenov_criticality(**{**DRUM, "mass_kg": 200.0})  # type: ignore[arg-type]
    assert bigger.critical_ambient_c < reference.critical_ambient_c


def test_a_package_with_no_crossover_in_range_is_told_which_end_it_ran_off() -> None:
    """Both ends of the bracket, and they mean opposite things.

    A clamped value returned as a critical ambient would be read as one — a transport decision made
    from -40 °C or 400 °C because the search stopped there. The two messages are distinguished
    because "self-heats in a freezer" and "no crossover below 400 °C" lead to opposite actions.
    """
    # A rate in the wrong unit *read at a low reference temperature* — which is the realistic way
    # to land here. The first attempt at this arm used 1e9 W/kg at the 100 °C reference and did not
    # raise: Arrhenius attenuates that by 1.7e-12 on the way down to -40 °C, so it takes 1.9e11 to
    # outrun a 2.5 W/K loss from there. The mistake that actually reaches this branch is not a large
    # number, it is a number attached to the wrong temperature.
    with pytest.raises(ThermalInputError, match="no stable ambient"):
        semenov_criticality(
            **{**DRUM, "heat_release_rate_w_per_kg": 1e6, "reference_temperature_c": -40.0}  # type: ignore[arg-type]
        )
    with pytest.raises(ThermalInputError, match="no crossover exists"):
        semenov_criticality(**{**DRUM, "heat_release_rate_w_per_kg": 1e-12})  # type: ignore[arg-type]


def test_heat_generation_extrapolates_along_arrhenius_in_both_directions() -> None:
    """The rate at the reference point is the rate given, and it falls as temperature falls.

    The identity at the reference temperature is the assertion that catches a reciprocal written
    the wrong way round — an error that leaves the extrapolation monotone and every derived number
    wrong by an exponential.
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
            semenov_criticality(**{**DRUM, field: 0.0})  # type: ignore[arg-type]
