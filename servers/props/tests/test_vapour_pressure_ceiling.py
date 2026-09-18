"""The refuse-rather-than-approximate rule was applied to one end of the temperature axis only.

`vapour_pressure` refused below the melting point, where there is no liquid — and had no upper
bound at all. Measured for toluene before this: 25 °C gave 0.038 bar by the Antoine fit, 400 °C gave
88.6 bar, 1000 °C gave 1,448 bar, and **5000 °C gave 15,606 bar**, all returned as
`method="clausius_clapeyron"` with the caveat "progressively optimistic away from it".

Above a critical temperature there is no liquid and therefore no vapour pressure, which is the
*same* argument that produces the refusal below the melting point — and the last two of those
numbers describe a substance that does not exist in the state the question assumes.

The corpus carries no critical temperature, so the bound is Guldberg's rule set deliberately loose:
a sanity ceiling rather than a phase boundary. Why *loose* is itself measured, and is pinned below
— at the textbook 1.5 the ceiling for water falls at 286.6 °C and refuses 300 °C water, which this
server answers at 98.0 bar against the steam-table 85.879 that `test_tools.py` already prints.
"""

from __future__ import annotations

import inspect

import pytest
from chemclaw_mcp_props.engine import correlations, records
from mcp_server_kit.testing import reimported


def test_a_temperature_above_the_estimated_critical_point_is_refused() -> None:
    """The three numbers this test names are the ones that used to come back as pressures."""
    toluene = records.require("toluene")
    for temperature_c in (600.0, 1000.0, 5000.0):
        with pytest.raises(ValueError, match="critical"):
            correlations.vapour_pressure(toluene, temperature_c)


def test_the_refusal_names_the_rule_and_the_temperature_it_stops_at() -> None:
    """A refusal a chemist cannot argue with is one that says where the bound came from."""
    toluene = records.require("toluene")
    with pytest.raises(ValueError) as refusal:
        correlations.vapour_pressure(toluene, 5000.0)
    message = str(refusal.value)
    assert "Guldberg" in message
    assert "CHEMCLAW_PROPS_MAX_TB_RATIO" in message
    assert f"{correlations.max_temperature_c(toluene):.1f}" in message


def test_the_ceiling_is_where_the_rule_puts_it() -> None:
    """1.5 x Tb in kelvin, converted back — arithmetic, so it is checked as arithmetic."""
    toluene = records.require("toluene")
    kelvin = correlations.KELVIN
    expected_c = correlations.MAX_TEMPERATURE_TB_RATIO * (toluene.bp_c + kelvin) - kelvin
    assert correlations.max_temperature_c(toluene) == pytest.approx(expected_c)
    # And it is above every temperature a distillation or a stripping question asks about.
    assert correlations.max_temperature_c(toluene) > toluene.bp_c + 150.0


def test_every_solvent_s_ceiling_leaves_room_above_its_boiling_point() -> None:
    """A bound that refused an ordinary vacuum-distillation question would be worse than none."""
    for solvent in records.all_solvents():
        ceiling = correlations.max_temperature_c(solvent)
        assert ceiling > solvent.bp_c, solvent.name
        # The distillation questions this server exists for sit under the boiling point, and a
        # pressurised one sits above it; 100 K of headroom is what keeps the second answerable.
        assert ceiling - solvent.bp_c > 100.0, solvent.name


def test_the_inverse_direction_stops_at_the_same_ceiling() -> None:
    """`boiling_point_at` brackets by evaluating `vapour_pressure`, so the two must agree.

    Its bracket used to run to `max(bp + 200, 400) °C` regardless — which is above the ceiling for
    every solvent boiling under about 176 °C, so an unclamped bracket would make the forward bound
    raise from inside the inverse and turn an ordinary refusal into an internal error.
    """
    toluene = records.require("toluene")
    ceiling = correlations.max_temperature_c(toluene)
    at_ceiling = correlations.vapour_pressure(toluene, ceiling)
    with pytest.raises(ValueError, match="never reaches"):
        correlations.boiling_point_at(toluene, at_ceiling.pressure_mbar * 2.0)
    # And a pressure it does reach still answers, from inside the bracket.
    answer = correlations.boiling_point_at(toluene, at_ceiling.pressure_mbar / 2.0)
    assert toluene.bp_c < answer < ceiling


def test_the_ceiling_is_loose_enough_not_to_refuse_a_real_question() -> None:
    """Why the ratio is 1.8 and not Guldberg's textbook 1.5, as a test rather than as prose.

    The ratio is one number across liquid families that do not share one, and water is the worst
    case in this table. At 1.5 its ceiling is 286.6 °C, which refuses 300 °C water — a question
    `test_tools.py` already drives, and which this server answers to within about 14% of the steam
    tables. A bound that refuses that is worse than the runaway it was added to stop.
    """
    water = records.require("water")
    guldberg_ceiling_c = 1.5 * (water.bp_c + correlations.KELVIN) - correlations.KELVIN
    assert guldberg_ceiling_c < 300.0
    assert correlations.max_temperature_c(water) > 300.0
    assert correlations.vapour_pressure(water, 300.0).pressure_bar > 0.0


def test_the_ratio_that_sets_the_ceiling_refuses_a_value_that_would_answer_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound behind the bound, driven through this module's own import.

    `CHEMCLAW_PROPS_MAX_TB_RATIO` is the last bound in this fleet that was read as a bare
    `float(os.environ.get(...))` after every integer one moved behind a reader that refuses at
    import. It had the same defect eight of those had, and a sharper one than most because it
    *multiplies* rather than caps. Measured on toluene before `env_ratio`:

        1.8 (the default)  ceiling  417.6 °C
        1.0                ceiling  110.6 °C — exactly the normal boiling point
        0                  ceiling -273.1 °C — below absolute zero; every question refused
        "loose"            ValueError: could not convert string to float: 'loose'

    A pod set to `0` started, passed its readiness probe, and refused every vapour-pressure
    question. Both directions are driven, because a reader that refuses everything is not a fix.
    """
    for unusable in ("0", "-1", "1.0", "loose"):
        monkeypatch.setenv("CHEMCLAW_PROPS_MAX_TB_RATIO", unusable)
        with pytest.raises(ValueError, match="CHEMCLAW_PROPS_MAX_TB_RATIO"):
            reimported(correlations)

    monkeypatch.setenv("CHEMCLAW_PROPS_MAX_TB_RATIO", "1.01")
    at_the_floor = reimported(correlations)
    toluene = records.require("toluene")
    ceiling = at_the_floor.max_temperature_c(toluene)
    assert ceiling > toluene.bp_c, (
        "the floor must leave a liquid range above the normal boiling point; at or below 1 the "
        "ceiling is the boiling point itself and the multiplier has stopped multiplying"
    )

    monkeypatch.delenv("CHEMCLAW_PROPS_MAX_TB_RATIO")
    assert pytest.approx(1.8) == reimported(correlations).MAX_TEMPERATURE_TB_RATIO, (
        "an unset variable must still be the default this module argues for"
    )


def test_the_ratio_can_still_be_loosened_which_is_what_the_knob_is_for() -> None:
    """The asymmetry with the fleet's crash bounds, asserted where the argument lives.

    `MAX_MOLECULE_ATOMS` has a ceiling because raising it re-arms an uncatchable SIGSEGV. This one
    has none on purpose: the comment beside it says a deployment holding a real critical
    temperature, or asking a supercritical question deliberately, changes it without editing code.
    A `maximum` here would contradict the decision the knob exists to serve, so the absence of one
    is asserted rather than left to be read off a docstring.
    """
    from mcp_server_kit import limits

    assert limits.env_ratio(
        "CHEMCLAW_PROPS_MAX_TB_RATIO_UNSET_IN_THIS_TEST",
        default=1.8,
        minimum=1.01,
        consequence="x",
    ) == pytest.approx(1.8)
    assert "maximum" not in inspect.signature(limits.env_ratio).parameters, (
        "env_ratio grew a ceiling; `servers/props`' own argument is that this ratio must stay "
        "loosenable, so that is a decision needing a record rather than a parameter"
    )
