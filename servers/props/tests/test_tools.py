"""What the tools answer, and — as much — what they refuse to answer.

The behaviours pinned here are the ones a wrong answer would be expensive for: a vapour pressure
that silently used the weaker correlation, a boiling point under vacuum that came out above the
atmospheric one, a swap shortlist that quietly promoted a worse hazard band.
"""

from __future__ import annotations

import math

import pytest
from chemclaw_mcp_props import tools
from chemclaw_mcp_props.engine import correlations, records


def test_solvent_properties_resolves_an_abbreviation() -> None:
    """Chemists write `2-MeTHF`, not `2-methyltetrahydrofuran`."""
    record = tools.solvent_properties("2-MeTHF")
    assert record.name == "2-methyltetrahydrofuran"
    assert record.boiling_point_c == 80.2
    assert record.peroxide_former is True
    assert record.source


def test_solvent_properties_keeps_an_absent_flash_point_absent() -> None:
    """`null` means "has no flash point". Zero would read as "flashes at 0 °C"."""
    assert tools.solvent_properties("DCM").flash_point_c is None


def test_an_unknown_solvent_is_refused_rather_than_approximated() -> None:
    """A substituted solvent would corrupt every number downstream of it."""
    with pytest.raises(ValueError, match="vendored solvent table"):
        tools.solvent_properties("unobtainium")


def test_vapour_pressure_at_the_boiling_point_is_one_atmosphere() -> None:
    """The anchor both correlations share — if this drifts, everything built on it has."""
    for name in ("toluene", "2-MeTHF", "DMSO"):
        solvent = records.require(name)
        result = tools.vapour_pressure(name, solvent.bp_c)
        assert math.isclose(result.pressure_mbar, 1013.25, rel_tol=0.03), name


def test_the_weaker_correlation_says_so() -> None:
    """A Trouton-estimated pressure must never arrive looking like a fitted one."""
    fitted = tools.vapour_pressure("toluene", 25.0)
    assert fitted.method == "antoine"
    estimated = tools.vapour_pressure("sulfolane", 100.0)
    assert estimated.method == "clausius_clapeyron_trouton"
    assert "Trouton" in estimated.caveat


def test_vapour_pressure_below_the_melting_point_is_refused() -> None:
    """Below the melting point the liquid vapour pressure is not the quantity being asked for."""
    with pytest.raises(ValueError, match="below the melting point"):
        tools.vapour_pressure("sulfolane", 10.0)


def test_boiling_point_falls_under_vacuum() -> None:
    """The everyday rotovap question, and the direction of the answer is the check."""
    result = tools.boiling_point_at_pressure("toluene", 100.0)
    assert result.boiling_point_c < result.normal_boiling_point_c
    assert 40.0 < result.boiling_point_c < 70.0


def test_boiling_point_at_atmospheric_pressure_is_the_tabulated_one() -> None:
    """The inverse must agree with the forward direction, or one of them is wrong."""
    result = tools.boiling_point_at_pressure("ethanol", 1013.25)
    assert abs(result.boiling_point_c - 78.4) < 2.0


def test_a_vacuum_that_freezes_the_solvent_is_refused() -> None:
    """Sulfolane melts at 27.5 °C; a deep vacuum reaches its boiling point below that."""
    with pytest.raises(ValueError, match="freezes in the still"):
        tools.boiling_point_at_pressure("sulfolane", 0.001)


def test_swap_candidates_never_silently_worsen_the_hazard_band() -> None:
    """The default filter is the point: a worse band is returned blocked, not ranked first."""
    result = tools.solvent_swap_candidates("ethyl acetate", top_n=10)
    for candidate in result.candidates:
        if candidate.passes_constraints:
            assert candidate.greenness_band == "recommended"
        else:
            assert candidate.blockers


def test_swap_candidates_report_what_a_constraint_cost() -> None:
    """ "X is closest but it is reprotoxic" is usually the most useful sentence in the answer.

    Asked of a `recommended` solvent and with `top_n` past the size of the recommended set, so the
    list is guaranteed to run into blocked candidates — and they must arrive *after* the passing
    ones, each carrying the constraint it failed rather than being silently dropped.
    """
    result = tools.solvent_swap_candidates("acetone", top_n=40)
    passing = [c for c in result.candidates if c.passes_constraints]
    blocked = [c for c in result.candidates if not c.passes_constraints]
    assert blocked, "some candidate should fail the default greenness filter"
    assert all(c.blockers for c in blocked)
    assert result.candidates[: len(passing)] == passing, "passing candidates must be ranked first"
    assert any("greenness band worsens" in blocker for c in blocked for blocker in c.blockers)


def test_swap_constraints_are_applied() -> None:
    """Each filter has to actually remove things, or it is decoration on the docstring."""
    result = tools.solvent_swap_candidates(
        "tetrahydrofuran",
        top_n=8,
        exclude_peroxide_formers=True,
        require_water_miscibility="immiscible",
    )
    for candidate in result.candidates:
        if candidate.passes_constraints:
            assert candidate.peroxide_former is False
            assert candidate.water_miscibility == "immiscible"


def test_a_swap_shortlist_states_its_own_basis() -> None:
    """Hansen distance is a solubility argument; the answer has to say so on its own."""
    result = tools.solvent_swap_candidates("N,N-dimethylformamide", top_n=3)
    assert "Hansen" in result.basis
    assert "reactivity" in result.basis


def test_compare_solvent_properties_reports_unknown_names_without_failing() -> None:
    """One typo must not cost the whole comparison."""
    result = tools.compare_solvent_properties(["THF", "2-MeTHF", "not-a-solvent"])
    assert [row.name for row in result.rows] == ["tetrahydrofuran", "2-methyltetrahydrofuran"]
    assert result.unknown == ["not-a-solvent"]


def test_list_solvents_covers_the_table() -> None:
    """The tool that tells the agent what this server can and cannot be asked about."""
    listed = tools.list_solvents()
    assert len(listed) == len(records.all_solvents())
    assert any(entry.name == "cyclopentyl methyl ether" for entry in listed)


def test_hansen_ranking_matches_chemical_intuition() -> None:
    """2-MeTHF is the textbook THF replacement; the ranking should not have to be argued with."""
    thf = records.require("tetrahydrofuran")
    ranked = sorted(
        (s for s in records.all_solvents() if s.name != thf.name),
        key=lambda s: correlations.hansen_distance(thf, s),
    )
    # Six, not five: correcting dimethyl carbonate's Hansen polar term to the published 3.9 moved
    # it from Ra 5.4 to Ra 3.59 against THF, which is genuinely one place closer than 2-MeTHF. The
    # claim being pinned is "near the top", and DCM, EtOAc and anisole were always ahead of it.
    assert "2-methyltetrahydrofuran" in [s.name for s in ranked[:6]]


def test_acetic_acid_answers_the_vacuum_distillation_question_correctly() -> None:
    """The defect that reaches a bench: a jacket temperature 32 °C low, and a false refusal.

    Acetic acid associates in the *vapour*, so its dHvap at the boiling point is far below the
    ambient value and a single-slope Clausius-Clapeyron extrapolation from it reads several times
    high. The literature anchors below are CRC vapour-pressure tables (10 mmHg at 17.1 °C, 20 at
    29.9, 40 at 43.0, 60 at 51.7, 100 at 63.0), log-interpolated.
    """
    for pressure_mbar, literature_c in ((100.0, 56.6), (50.0, 41.8)):
        result = tools.boiling_point_at_pressure("AcOH", pressure_mbar)
        print(
            f"acetic acid at {pressure_mbar:6.1f} mbar -> model {result.boiling_point_c:6.2f} °C  "
            f"literature {literature_c:6.2f} °C  method={result.method}"
        )
        assert abs(result.boiling_point_c - literature_c) < 2.5, (
            f"at {pressure_mbar} mbar the model says {result.boiling_point_c} °C against a "
            f"literature {literature_c} °C"
        )
    forward = tools.vapour_pressure("AcOH", 20.0)
    print(f"acetic acid at 20 °C -> model {forward.pressure_mbar:8.2f} mbar  literature 15.70 mbar")
    assert abs(forward.pressure_mbar - 15.7) / 15.7 < 0.15


def test_a_pressure_the_solvent_cannot_reach_is_refused_rather_than_bracketed() -> None:
    """The bisection ceiling is not a boiling point, and must never be returned as one.

    DMSO's vapour pressure at the top of the bracket is about 28 bar, so a 30 bar autoclave has no
    answer in this correlation — and DMSO decomposes exothermically well below 400 °C anyway.
    """
    for name, pressure_mbar in (("DMSO", 30_000.0), ("sulfolane", 25_000.0)):
        solvent = records.require(name)
        ceiling_c = max(solvent.bp_c + 200.0, 400.0)
        ceiling = tools.vapour_pressure(name, ceiling_c)
        print(
            f"{solvent.name:22s} bracket ceiling {ceiling_c:6.1f} °C, vapour pressure there "
            f"{ceiling.pressure_mbar / 1000:7.2f} bar -> asked for {pressure_mbar / 1000:6.2f} bar"
        )
        assert ceiling.pressure_mbar < pressure_mbar, "the example must be genuinely unreachable"
        with pytest.raises(ValueError, match="never reaches"):
            tools.boiling_point_at_pressure(name, pressure_mbar)


def test_antoine_is_not_extrapolated_past_the_range_it_was_fitted_over() -> None:
    """The module docstring promised this fallback; until it was written it did not exist.

    Water's Stull constants are fitted to 255.9-373 K. At 200 °C they read 13.775 bar against the
    steam-table 15.549 bar (-11.4%) while still calling themselves `antoine`, and the error grows
    monotonically from there.
    """
    steam_table_bar = {150.0: 4.760, 200.0: 15.549, 300.0: 85.879}
    for temperature_c, truth in steam_table_bar.items():
        result = tools.vapour_pressure("water", temperature_c)
        error = 100.0 * (result.pressure_bar - truth) / truth
        print(
            f"water at {temperature_c:6.1f} °C -> {result.pressure_bar:8.3f} bar  "
            f"steam table {truth:8.3f} bar  err {error:+6.1f}%  method={result.method}"
        )
        assert result.method != "antoine", (
            f"{temperature_c} °C is outside the fitted range and must not be labelled a fit"
        )
    inside = tools.vapour_pressure("water", 60.0)
    assert inside.method == "antoine"
    assert "255.9" in inside.caveat and "373.0" in inside.caveat


def test_the_hansen_polar_term_of_dimethyl_carbonate_is_the_published_one() -> None:
    """dP = 8.6 made DMC the 2nd-closest acetone replacement. The published value is 3.9, and 13th.

    A wrong middle Hansen term is invisible to every other check in this corpus — the triple is
    transcribed as a unit — and it reaches a chemist as a `recommended`-band, not-ICH-listed
    solvent presented as the near-best match for acetone. `test_dataset` now screens the column
    against Beerbower; this pins the consequence.
    """
    acetone, dmc = records.require("acetone"), records.require("DMC")
    assert dmc.hansen_p == 3.9
    ranked = sorted(
        (s for s in records.all_solvents() if s.name != acetone.name),
        key=lambda s: correlations.hansen_distance(acetone, s),
    )
    place = [s.name for s in ranked].index(dmc.name) + 1
    print(
        f"replacing acetone: Ra(DMC) = {correlations.hansen_distance(acetone, dmc):.2f} at rank "
        f"{place} (with the old dP = 8.6 it was Ra 3.24 at rank 2)"
    )
    assert abs(correlations.hansen_distance(acetone, dmc) - 7.04) < 0.05
    assert place > 10
