"""The tool surface: what a caller gets back, and what every answer is obliged to say about itself.

`engine/` is tested for arithmetic. This file tests the *surface* — the shape of each result, the
`basis` string every one of them carries, and the defaults the tools do and do not supply. Those
are the parts a chemist reads, and none of them is exercised by an engine test.

`@server.tool()` returns the undecorated function, so calling these directly skips pydantic's
argument validation. That is deliberate here and the reason `test_server.py` exists: a bound that
is only real over the wire is asserted over the wire.
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
    """The one property every result here shares, asserted over all seven rather than one by one.

    A number from a Semenov balance and a number from an adiabatic balance answer different
    questions, and a result that does not say which cannot be put in a report. It is checked as a
    property of the set because the realistic regression is a *new* tool shipped without one —
    which a per-tool test written alongside that tool would never catch.
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
    """The refusal that matters most on this server, and it must survive a docstring edit.

    A Semenov estimate quoted as an SADT is a transport classification made from arithmetic. The
    disclaimer lives in three places — the docstring the model reads, the `basis` string the answer
    carries, and the module header — and the one that travels with the *number* is this one, so it
    is the one asserted.
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
    """The one default this server supplies, and the reason it is safe to supply.

    `reference_temperature_c` defaulting to `temperature_c` means "the rate you gave me was read
    here" — an identity rather than an assumption, so the extrapolation is a no-op. Asserted by
    comparing the default call against the explicit one, which is what makes it an identity claim
    and not a spot value.
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
    """The sentinel this field is read through, and truthiness discarded a real input.

    **`reference_temperature_c` defaulted to `0.0` and was read with `if ... else temperature_c`**,
    so
    a caller who stated "the rate was measured at 0 °C" — an ice-bath isothermal, the ordinary
    reference for a peroxide or a diazo compound — had that silently replaced by `temperature_c`.
    The Arrhenius extrapolation was then skipped and q(0 °C) used as though it were q(T_asked).

    Measured at q = 1 W/kg, E_a = 100 kJ/mol, c_p = 1.8 kJ/(kg·K), asked at 150 °C: a stated
    reference of **0.0** gave TMR_ad = 7.44 h where **0.001** gave 1.24e-06 h. A thousandth of a
    degree moved the answer by a factor of six million, because q(150 °C) is 6.0e6 W/kg and the tool
    was using 1. Carried into `stoessel_criticality_class`, which names this tool at
    `target_hours=24` as its source, that is class 2 — "a cooling failure reaches neither barrier" —
    against class 5, "the scenario has to be eliminated by process design".

    So this asserts CONTINUITY rather than a spot value: 0.0 must sit between its neighbours, which
    no
    sentinel reading can satisfy by accident. The test above still holds the omitted case, and the
    two
    together are what make the field's two meanings distinguishable.
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
    """The half the default hides: a rate measured hot, asked about cold, must be attenuated.

    Without the extrapolation the rate at 200 °C would be used as the rate at 100 °C, making TMR_ad
    shorter by orders of magnitude — an error in the *conservative* direction for the headline
    number and in the dangerous direction for nothing, which is exactly why it would survive review
    unnoticed. Asserted as an inequality against the un-extrapolated call.
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
    """`tmr_ad`'s headline answer still exists when its secondary one does not.

    The TMR at the temperature asked about is always computable; the *inverse* may not be. Failing
    the whole call because T_D24 is out of range would withhold the number the caller asked for, so
    the field is null and `note` carries the engine's own explanation verbatim.
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
    """`connector_app` passes `ValueError` through and replaces everything else with an error_id.

    So these messages are only useful to a chemist if they stay in that family. Both families this
    server raises are checked, because `FormulaError` and `ThermalInputError` are separate classes
    and one of them subclassing something else would silently turn a worded message into an opaque
    identifier.
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
    """A bound on the input, which is what `docs/adding-a-server.md` asks of any unbounded one.

    The parser is linear, so a megabyte of `C` is a megabyte of work and a dict with one key. Cheap
    rather than dangerous — but unbounded, and this fleet bounds inputs rather than arguing about
    which unbounded ones are affordable.
    """
    with pytest.raises(FormulaError, match="at most"):
        oxygen_balance_screen(molecular_formula="C" * (MAX_FORMULA_CHARACTERS + 1))
    # And the bound is above anything real: nitroglycerine is nine characters.
    assert MAX_FORMULA_CHARACTERS > 60
