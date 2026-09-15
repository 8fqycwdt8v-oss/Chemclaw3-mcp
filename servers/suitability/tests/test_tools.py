"""The tool surface: what it answers, what it refuses, and what every answer carries with it.

The engine tests cover the arithmetic. What is left for this file is the layer between the
arithmetic and the model — the `basis` contract, the composition in the report, and the boundary
where a tool declines to invent a criterion nobody declared.

Note that `@server.tool()` returns the undecorated function, so calls here skip pydantic argument
validation. Anything that is only enforced by the schema is asserted in `test_server.py`, over the
wire, where a caller actually meets it.
"""

from __future__ import annotations

import pytest
from chemclaw_mcp_suitability import tools


def test_every_tool_returns_a_basis_that_names_its_formula() -> None:
    """Asserted over the set, so a *new* tool without a basis fails here rather than in review.

    A number without the model it came out of is not something anybody can put in a report, and on
    this server it is worse than that: the same peak has two plate counts and two resolutions, so
    a bare number is not even comparable with the limit it is being checked against.
    """
    answers = [
        tools.replicate_precision([100.0, 101.0, 99.0]),
        tools.plate_count(6.0, 0.2, "tangent"),
        tools.peak_symmetry(0.4, 0.17),
        tools.peak_resolution(5.0, 5.4, 0.2, 0.21, "tangent"),
        tools.retention_factor(6.0, 1.2),
        tools.permitted_method_adjustment("flow_rate", 1.0, 1.2),
        tools.system_suitability_report(
            [tools.PeakInput(name="main", retention_time_min=4.0, width_min=0.2)], "tangent", [], []
        ),
    ]
    for answer in answers:
        assert answer.basis, f"{type(answer).__name__} carries no basis"
        assert "USP" in answer.basis or "<621>" in answer.basis


def test_the_basis_names_the_convention_that_was_actually_used() -> None:
    """Not just "USP <621>" — *which* form, since that is the part that makes it comparable."""
    assert "16" in tools.plate_count(6.0, 0.2, "tangent").basis
    assert "5.54" in tools.plate_count(6.0, 0.2, "half_height").basis
    assert "1.18" in tools.peak_resolution(5.0, 5.4, 0.2, 0.2, "half_height").basis


def test_the_adjustment_basis_says_a_monograph_overrides_it() -> None:
    """The limit on what that answer means, carried with the answer rather than in a docstring.

    <621>'s section opens "unless otherwise specified in the individual monograph", and this
    server cannot read the monograph. A model that is not told will present a general-chapter
    allowance as the applicable one.
    """
    basis = tools.permitted_method_adjustment("flow_rate", 1.0, 1.2).basis
    assert "monograph" in basis
    assert "isocratic" in basis


def test_the_report_introduces_no_arithmetic_of_its_own() -> None:
    """Every number in the report equals what the standalone tool gives for the same input.

    The property that makes the composite safe to offer beside the primitives: if it drifted, a
    chemist would get two different answers to one question depending on which tool the model
    happened to call.
    """
    peaks = [
        tools.PeakInput(
            name="A",
            retention_time_min=3.1,
            width_min=0.18,
            width_at_5_percent_min=0.36,
            leading_half_width_min=0.15,
        ),
        tools.PeakInput(
            name="B",
            retention_time_min=4.09,
            width_min=0.20,
            width_at_5_percent_min=0.46,
            leading_half_width_min=0.10,
        ),
    ]
    areas = [1024110.0, 1038902.0, 1001455.0, 1061203.0, 998774.0, 1049318.0]
    report = tools.system_suitability_report(
        peaks, "tangent", areas, [], area_rsd_limit_percent=2.0
    )

    assert report.peaks[0].plates == pytest.approx(tools.plate_count(3.1, 0.18, "tangent").plates)
    assert report.peaks[1].tailing_factor == pytest.approx(
        tools.peak_symmetry(0.46, 0.10).tailing_factor
    )
    assert report.peaks[1].resolution_from_previous == pytest.approx(
        tools.peak_resolution(3.1, 4.09, 0.18, 0.20, "tangent").resolution
    )
    assert report.area_precision is not None
    assert report.area_precision.relative_standard_deviation_percent == pytest.approx(
        tools.replicate_precision(areas, 2.0).relative_standard_deviation_percent
    )


def test_an_undeclared_criterion_is_not_checked_and_does_not_appear_in_failures() -> None:
    """`failures: []` means "every declared criterion passed", never "this run is suitable".

    The dangerous reading, so it is pinned: a table whose resolution is 0.49 — badly co-eluting —
    reports no failure when no minimum resolution was declared, and reports one the moment it is.
    """
    peaks = [
        tools.PeakInput(name="A", retention_time_min=4.00, width_min=0.40),
        tools.PeakInput(name="B", retention_time_min=4.10, width_min=0.41),
    ]
    silent = tools.system_suitability_report(peaks, "tangent", [], [])
    assert silent.failures == []
    assert silent.peaks[1].resolution_from_previous is not None
    assert silent.peaks[1].resolution_from_previous < 0.5

    declared = tools.system_suitability_report(peaks, "tangent", [], [], minimum_resolution=2.0)
    assert len(declared.failures) == 1
    assert "resolution" in declared.failures[0]


def test_the_report_flags_a_short_series_separately_from_a_failed_limit() -> None:
    """Two independent verdicts a single pass/fail would conflate.

    Five injections at a 3.0% limit: the RSD passes and the series is one short of what <621>
    requires for a limit that size. Both matter and they are different remedies — one is re-develop
    the method, the other is inject again.
    """
    report = tools.system_suitability_report(
        [tools.PeakInput(name="main", retention_time_min=4.0)],
        "tangent",
        [100.0, 101.0, 99.0, 100.5, 99.5],
        [],
        area_rsd_limit_percent=3.0,
    )
    assert report.area_precision is not None
    assert report.area_precision.meets_limit is True
    assert len(report.failures) == 1
    assert "requires 6" in report.failures[0]


def test_a_report_with_no_replicates_returns_absent_precision_rather_than_failing() -> None:
    """Asking for peak metrics alone must not fail because no replicate series was pasted."""
    report = tools.system_suitability_report(
        [tools.PeakInput(name="main", retention_time_min=4.0, width_min=0.2)], "tangent", [], []
    )
    assert report.area_precision is None
    assert report.retention_precision is None
    assert report.peaks[0].plates is not None


def test_a_peak_with_no_width_reports_absent_metrics_rather_than_zero() -> None:
    """A retention time alone is a legitimate row, and `0.0` plates would read as a measurement."""
    report = tools.system_suitability_report(
        [tools.PeakInput(name="main", retention_time_min=4.0)], "tangent", [], []
    )
    assert report.peaks[0].plates is None
    assert report.peaks[0].tailing_factor is None
    assert report.peaks[0].resolution_from_previous is None


def test_the_report_refuses_more_peaks_than_it_prices() -> None:
    """A bound on the input, which `docs/adding-a-server.md` asks of a tool taking a list."""
    too_many = [
        tools.PeakInput(name=f"p{index}", retention_time_min=1.0 + index * 0.01)
        for index in range(tools.MAX_PEAKS + 1)
    ]
    with pytest.raises(ValueError, match="more than this tool will take"):
        tools.system_suitability_report(too_many, "tangent", [], [])


def test_the_retention_tool_returns_alpha_only_when_a_second_peak_was_given() -> None:
    """`None` rather than 1.0, because "one peak" and "two identical peaks" are different facts."""
    single = tools.retention_factor(6.0, 1.2)
    assert single.separation_factor is None
    assert single.second_retention_factor is None

    pair = tools.retention_factor(6.0, 1.2, 7.5)
    assert pair.separation_factor is not None
    assert pair.separation_factor > 1.0
