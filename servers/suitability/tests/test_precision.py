"""Replicate precision, and the <621> injection-count rule that is easy to get backwards.

The RSD itself is one line, so most of what is worth asserting here is the surrounding judgement:
which denominator, what happens at the boundary of the injection rule, and what the tool refuses.
"""

from __future__ import annotations

import math

import pytest
from chemclaw_mcp_suitability.engine import precision


def test_the_rsd_uses_the_sample_denominator_which_is_what_the_criterion_is_written_against() -> (
    None
):
    """The RSD uses the sample (n-1) denominator, which acceptance limits are written against.

    [1,2,3,4,5] gives 52.705% (sample) against 47.140% (population); both are stated so the
    difference is visible.
    """
    result = precision.relative_standard_deviation([1.0, 2.0, 3.0, 4.0, 5.0])
    assert result.relative_standard_deviation_percent == pytest.approx(
        100.0 * math.sqrt(2.5) / 3.0, rel=1e-12
    )
    assert result.relative_standard_deviation_percent != pytest.approx(
        100.0 * math.sqrt(2.0) / 3.0, rel=1e-6
    )
    assert result.standard_deviation == pytest.approx(math.sqrt(2.5), rel=1e-12)


def test_the_denominator_matters_by_nearly_nine_percent_at_six_injections() -> None:
    """Why the choice is worth a test rather than a comment, as a number.

    sqrt(n/(n-1)) at n = 6 is 1.0954, so the population form understates the RSD by 8.7% — the
    difference between passing and failing a 2.0% limit at a true 2.19%.
    """
    values = [100.0, 102.0, 98.0, 101.0, 99.0, 103.0]
    sample = precision.relative_standard_deviation(values).relative_standard_deviation_percent
    mean = sum(values) / len(values)
    population = 100.0 * math.sqrt(sum((v - mean) ** 2 for v in values) / len(values)) / mean
    assert (sample - population) / sample == pytest.approx(1.0 - math.sqrt(5.0 / 6.0), rel=1e-9)
    assert (sample - population) / sample == pytest.approx(0.0871, abs=0.0005)


def test_the_injection_rule_runs_the_counter_intuitive_way_round() -> None:
    """A tighter limit takes fewer injections; both sides of the 2.0% boundary are pinned.

    This catches a "fix" to the intuitive direction, the realistic edit.
    """
    assert precision.injections_required_for(0.5) == 5
    assert precision.injections_required_for(2.0) == 5  # at the boundary, inclusive
    assert precision.injections_required_for(2.01) == 6
    assert precision.injections_required_for(5.0) == 6


def test_a_series_that_meets_its_limit_on_too_few_injections_is_flagged() -> None:
    """A series that meets its limit on too few injections is flagged.

    At a 3.0% limit five injections pass the RSD yet fall short of what <621> requires for that
    limit; data systems do not check this.
    """
    result = precision.relative_standard_deviation([100.0, 101.0, 99.0, 100.5, 99.5], 3.0)
    assert result.meets_limit is True
    assert result.injections == 5
    assert result.injections_required == 6
    assert result.injections_are_sufficient is False


def test_no_declared_limit_means_no_verdict_rather_than_a_default_of_two_percent() -> None:
    """2.0% is an assay's convention; a related-substances method's limit is not 2.0%.

    Defaulting would put a verdict on a criterion nobody declared, which is the failure mode the
    report's own `failures` contract is written against.
    """
    result = precision.relative_standard_deviation([100.0, 101.0, 99.0])
    assert result.meets_limit is None
    assert result.injections_required is None
    assert result.injections_are_sufficient is None


def test_a_zero_mean_is_refused_because_the_rsd_is_undefined_rather_than_large() -> None:
    """A series averaging zero is a baseline or a sign error; dividing by it publishes infinity."""
    with pytest.raises(precision.PrecisionError, match="undefined rather than large"):
        precision.relative_standard_deviation([-1.0, 1.0])


def test_a_single_injection_is_refused_rather_than_answered_with_zero() -> None:
    """n-1 is zero, so there is no spread to measure — and 0.0% would read as perfect precision."""
    with pytest.raises(precision.PrecisionError, match="at least 2 injections"):
        precision.relative_standard_deviation([100.0])


def test_a_non_finite_value_is_refused_naming_which_injection() -> None:
    """A NaN from a spreadsheet propagates silently through a mean; the index is what locates it."""
    with pytest.raises(precision.PrecisionError, match="injection 3"):
        precision.relative_standard_deviation([100.0, 101.0, float("nan")])


def test_a_limit_of_zero_or_below_is_refused_with_the_units_mistake_named() -> None:
    """The realistic error is 0.02 entered where 2.0 was meant, which no series can ever meet."""
    with pytest.raises(precision.PrecisionError, match="in percent"):
        precision.injections_required_for(0.0)


def test_a_negative_mean_gives_a_positive_rsd() -> None:
    """A subtracted-baseline or RI detector can give one, and a negative RSD would read as a bug."""
    result = precision.relative_standard_deviation([-100.0, -102.0, -98.0])
    assert result.mean < 0.0
    assert result.relative_standard_deviation_percent > 0.0


def test_the_eval_corpus_six_injections_compute_to_what_a_data_system_would_report() -> None:
    """The six injections from Chemclaw3's `an-04` probe compute to what a data system would report.

    The probe's hand-written "2.4%" matches neither the sample (2.476%) nor population (2.260%)
    form, which is the arithmetic this server exists to take off a chemist and a model.
    """
    areas = [1024110.0, 1038902.0, 1001455.0, 1061203.0, 998774.0, 1049318.0]
    result = precision.relative_standard_deviation(areas, limit_percent=2.0)
    assert result.relative_standard_deviation_percent == pytest.approx(2.476, abs=0.001)
    assert result.meets_limit is False
    # Six injections for a 2.0% limit is more than <621> requires, so the series is sufficient
    # even though it fails — two independent verdicts that a single "pass/fail" would conflate.
    assert result.injections_required == 5
    assert result.injections_are_sufficient is True
