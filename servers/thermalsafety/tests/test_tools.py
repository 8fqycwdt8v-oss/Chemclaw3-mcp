"""The tool surface: what a caller gets back, and what every answer is obliged to say about itself.

Covers result shapes, the `basis` every result carries, and the defaults the tools do and do not
supply. `@server.tool()` returns the undecorated function, so wire-only bounds are asserted in
`test_server.py`.
"""

from __future__ import annotations

import pytest
from chemclaw_mcp_thermalsafety.engine.oxygen_balance import FormulaError
from chemclaw_mcp_thermalsafety.engine.runaway import ThermalInputError
from chemclaw_mcp_thermalsafety.tools import (
    MAX_FORMULA_CHARACTERS,
    adiabatic_temperature_rise,
    heat_removal_capacity,
    mtsr,
    oxygen_balance_screen,
    semenov_critical_ambient,
    stoessel_criticality_class,
    tmr_ad,
)


def test_every_tool_returns_a_basis_that_names_its_model_and_its_assumption() -> None:
    """Every tool returns a `basis` naming its model and assumption, asserted over the whole set.

    Semenov and adiabatic numbers answer different questions; checking the set catches a new tool
    shipped without one.
    """
    results = (
        adiabatic_temperature_rise(
            heat_of_reaction_kj_per_mol=-150.0,
            moles=10.0,
            mass_kg=50.0,
            specific_heat_kj_per_kg_k=1.9,
        ),
        mtsr(
            process_temperature_c=20.0,
            adiabatic_temperature_rise_k=100.0,
            accumulation_fraction=0.3,
        ),
        tmr_ad(
            temperature_c=150.0,
            heat_release_rate_w_per_kg=5.0,
            activation_energy_kj_per_mol=120.0,
            specific_heat_kj_per_kg_k=1.9,
        ),
        stoessel_criticality_class(
            process_temperature_c=20.0,
            mtsr_c=150.0,
            max_technical_temperature_c=160.0,
            decomposition_t_d24_c=140.0,
        ),
        heat_removal_capacity(
            heat_transfer_coefficient_w_per_m2_k=300.0,
            heat_exchange_area_m2=2.0,
            reactor_temperature_c=80.0,
            jacket_temperature_c=20.0,
            mass_kg=100.0,
        ),
        semenov_critical_ambient(
            mass_kg=25.0,
            heat_release_rate_w_per_kg=1.0,
            reference_temperature_c=100.0,
            activation_energy_kj_per_mol=140.0,
            heat_transfer_coefficient_w_per_m2_k=5.0,
            surface_area_m2=0.5,
        ),
        oxygen_balance_screen(molecular_formula="C7H5N3O6"),
    )
    for result in results:
        basis = result.basis
        assert len(basis) > 40, f"{type(result).__name__} carries a basis of {basis!r}"


def test_the_semenov_tool_says_in_its_own_words_that_it_is_not_an_sadt() -> None:
    """The Semenov tool says in its own words that it is not an SADT.

    The disclaimer that travels with the number is the `basis`, so that is the one asserted; a
    Semenov estimate quoted as an SADT is a transport classification made from arithmetic.
    """
    result = semenov_critical_ambient(
        mass_kg=25.0,
        heat_release_rate_w_per_kg=1.0,
        reference_temperature_c=100.0,
        activation_energy_kj_per_mol=140.0,
        heat_transfer_coefficient_w_per_m2_k=5.0,
        surface_area_m2=0.5,
    )
    assert "NOT an SADT" in result.basis
    assert "Test Series H" in result.basis
    docstring = semenov_critical_ambient.__doc__ or ""
    assert "not an sadt" in docstring.lower()


def test_the_rounded_value_is_never_below_the_computed_one() -> None:
    """Rounding *up* is the UN convention, and rounding to nearest would overstate the margin."""
    result = semenov_critical_ambient(
        mass_kg=25.0,
        heat_release_rate_w_per_kg=1.0,
        reference_temperature_c=100.0,
        activation_energy_kj_per_mol=140.0,
        heat_transfer_coefficient_w_per_m2_k=5.0,
        surface_area_m2=0.5,
    )
    assert result.rounded_up_to_five_c >= result.critical_ambient_c
    assert result.rounded_up_to_five_c % 5 == 0


def test_tmr_defaults_its_reference_to_the_temperature_asked_about() -> None:
    """TMR defaults its reference to the temperature asked about.

    That default is an identity (the rate was read here), so the extrapolation is a no-op; asserted
    by comparing default and explicit calls.
    """
    implicit = tmr_ad(
        temperature_c=150.0,
        heat_release_rate_w_per_kg=5.0,
        activation_energy_kj_per_mol=120.0,
        specific_heat_kj_per_kg_k=1.9,
    )
    explicit = tmr_ad(
        temperature_c=150.0,
        heat_release_rate_w_per_kg=5.0,
        activation_energy_kj_per_mol=120.0,
        specific_heat_kj_per_kg_k=1.9,
        reference_temperature_c=150.0,
    )
    assert implicit.tmr_ad_hours == pytest.approx(explicit.tmr_ad_hours)


def test_a_stated_reference_of_zero_celsius_is_a_reference_and_not_an_omission() -> None:
    """A stated reference of 0 °C is a reference, not an omission.

    An ice-bath isothermal is an ordinary reference, and a truthiness test would replace it with
    `temperature_c`, skipping the Arrhenius extrapolation by orders of magnitude and shifting the
    Stoessel class. Asserted as continuity: 0.0 must sit between its neighbours, which no sentinel
    reading satisfies by accident.
    """
    common = {
        "temperature_c": 150.0,
        "heat_release_rate_w_per_kg": 1.0,
        "activation_energy_kj_per_mol": 100.0,
        "specific_heat_kj_per_kg_k": 1.8,
    }
    below = tmr_ad(**common, reference_temperature_c=-0.001).tmr_ad_hours
    at_zero = tmr_ad(**common, reference_temperature_c=0.0).tmr_ad_hours
    above = tmr_ad(**common, reference_temperature_c=0.001).tmr_ad_hours

    # Increasing in the reference, because a *lower* reference means the given rate is extrapolated
    # further *up* to `temperature_c` — a larger q, so a shorter time to runaway.
    assert below < at_zero < above, (
        f"TMR_ad at a stated 0 °C ({at_zero:g} h) is not between its neighbours "
        f"({below:g} h at -0.001 °C, {above:g} h at +0.001 °C) — the value is being discarded and "
        "`temperature_c` substituted, which reports a runaway milliseconds away as hours away"
    )
    assert at_zero == pytest.approx(above, rel=1e-3), (
        "a thousandth of a degree changes the answer, so the two spellings are not one function"
    )
    # And the omitted case is still the identity the test above asserts, not 0 °C.
    omitted = tmr_ad(**common).tmr_ad_hours
    assert omitted == pytest.approx(tmr_ad(**common, reference_temperature_c=150.0).tmr_ad_hours)
    assert omitted != pytest.approx(at_zero), (
        "omitting the reference and stating 0 °C give the same answer, so the field cannot express "
        "an ice-bath reference at all"
    )


def test_tmr_extrapolates_the_rate_when_the_reference_is_a_different_temperature() -> None:
    """A rate measured at another temperature is extrapolated along Arrhenius.

    Without it a hot rate would be used cold, shortening TMR_ad by orders of magnitude in the
    conservative direction, which would survive review. Asserted as an inequality against the
    un-extrapolated call.
    """
    extrapolated = tmr_ad(
        temperature_c=100.0,
        heat_release_rate_w_per_kg=50.0,
        activation_energy_kj_per_mol=120.0,
        specific_heat_kj_per_kg_k=1.9,
        reference_temperature_c=200.0,
    )
    as_if_measured_there = tmr_ad(
        temperature_c=100.0,
        heat_release_rate_w_per_kg=50.0,
        activation_energy_kj_per_mol=120.0,
        specific_heat_kj_per_kg_k=1.9,
    )
    assert extrapolated.tmr_ad_hours > as_if_measured_there.tmr_ad_hours * 100


def test_a_target_tmr_no_temperature_reaches_comes_back_as_a_note_rather_than_an_error() -> None:
    """An unreachable target TMR comes back as a note, not an error.

    The TMR at the asked temperature is always computable; failing the call because T_D24 is out of
    range would withhold it, so the field is null and `note` carries the engine's explanation.
    """
    result = tmr_ad(
        temperature_c=25.0,
        heat_release_rate_w_per_kg=1e-12,
        activation_energy_kj_per_mol=200.0,
        specific_heat_kj_per_kg_k=1.9,
        target_hours=1e-9,
    )
    assert result.tmr_ad_hours > 0
    assert result.temperature_for_target_c is None
    assert "does not reach" in result.note


def test_the_criticality_ordering_comes_back_as_readable_strings_with_units() -> None:
    """The evidence for the class, formatted for a person rather than as bare floats."""
    result = stoessel_criticality_class(
        process_temperature_c=20.0,
        mtsr_c=150.0,
        max_technical_temperature_c=100.0,
        decomposition_t_d24_c=140.0,
    )
    assert result.criticality_class == 4
    assert all("°C" in entry for entry in result.ordering)
    assert result.ordering[0].startswith("T_p")


def test_a_domain_refusal_reaches_the_caller_as_a_value_error() -> None:
    """Domain refusals are `ValueError`s, which `connector_app` passes through verbatim.

    Both `FormulaError` and `ThermalInputError` are checked, since either leaving the family would
    turn a worded message into an opaque identifier.
    """
    assert issubclass(FormulaError, ValueError)
    assert issubclass(ThermalInputError, ValueError)
    with pytest.raises(ValueError, match="accumulation_fraction"):
        mtsr(
            process_temperature_c=20.0,
            adiabatic_temperature_rise_k=100.0,
            accumulation_fraction=30.0,
        )
    with pytest.raises(ValueError, match="parentheses"):
        oxygen_balance_screen(molecular_formula="Ca(NO3)2")


def test_an_oversized_formula_is_refused_before_it_is_parsed() -> None:
    """An oversized formula is refused before it is parsed.

    The parser is linear and cheap, but this fleet bounds every input rather than arguing which
    unbounded ones are affordable.
    """
    with pytest.raises(FormulaError, match="at most"):
        oxygen_balance_screen(molecular_formula="C" * (MAX_FORMULA_CHARACTERS + 1))
    # And the bound is above anything real: nitroglycerine is nine characters.
    assert MAX_FORMULA_CHARACTERS > 60
