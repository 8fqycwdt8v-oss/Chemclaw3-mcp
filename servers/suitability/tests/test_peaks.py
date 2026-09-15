"""The per-peak metrics, checked against relations that hold independently of this code.

The strongest tests here are not "does 16(t/W)^2 return what 16(t/W)^2 returns" — that asserts the
code against itself. They are the ones that use the fact that **USP's constants over-determine each
other**: 5.54 was derived from 16 through the Gaussian width relation, and 1.18 from 2 the same way,
so on a Gaussian peak the two forms of each pair must agree. That catches a transposed digit, which
a self-referential test cannot.
"""

from __future__ import annotations

import math

import pytest
from chemclaw_mcp_suitability.engine import peaks

#: A Gaussian peak's tangent width is 4 sigma; its half-height width is 2 sqrt(2 ln 2) sigma.
TANGENT_PER_SIGMA = 4.0
HALF_HEIGHT_PER_SIGMA = 2.0 * math.sqrt(2.0 * math.log(2.0))


def test_the_two_plate_conventions_agree_on_a_gaussian_peak() -> None:
    """5.54 came from 16 through the Gaussian width relation, so they must agree to the rounding.

    16/4^2 is exactly 1; 5.54/(2 sqrt(2 ln 2))^2 is 0.99909, because the published 5.54 is 5.545
    rounded. So the disagreement is 0.09%, and it is arithmetic rather than a tolerance chosen
    until this passed. The nearest transposition, 5.45, would be off by 1.7% and fail here.
    """
    sigma, retention = 0.05, 6.0
    tangent = peaks.plate_count(retention, TANGENT_PER_SIGMA * sigma, "tangent")
    half = peaks.plate_count(retention, HALF_HEIGHT_PER_SIGMA * sigma, "half_height")
    assert tangent.plates == pytest.approx(half.plates, rel=1e-3)
    # And the exact value is checkable without either constant: N = (t/sigma)^2 for a Gaussian.
    assert tangent.plates == pytest.approx((retention / sigma) ** 2, rel=1e-9)


def test_the_two_resolution_conventions_agree_on_a_pair_of_gaussian_peaks() -> None:
    """1.18 came from 2 the same way: 2/8 = 0.25 against 1.18/(2 x 2.3548) = 0.25054."""
    sigma = 0.04
    tangent = peaks.resolution(
        5.0, 5.4, TANGENT_PER_SIGMA * sigma, TANGENT_PER_SIGMA * sigma, "tangent"
    )
    half = peaks.resolution(
        5.0, 5.4, HALF_HEIGHT_PER_SIGMA * sigma, HALF_HEIGHT_PER_SIGMA * sigma, "half_height"
    )
    assert tangent.resolution == pytest.approx(half.resolution, rel=3e-3)


def test_the_half_height_answer_says_it_assumed_a_gaussian_and_the_tangent_one_does_not() -> None:
    """The flag is the whole point of reporting the convention: one form is a definition.

    On a tailing peak the half-height form overstates resolution, and it is the one a data system
    reports by default — so a caller who is not told cannot know the number is the optimistic one.
    """
    common = (5.0, 5.4, 0.2, 0.21)
    assert peaks.resolution(*common, "half_height").assumes_gaussian is True
    assert peaks.resolution(*common, "tangent").assumes_gaussian is False


def test_the_two_conventions_diverge_on_a_tailing_peak_which_is_why_both_are_offered() -> None:
    """The reason `convention` has no default, stated as a measurement rather than a warning.

    A peak whose half-height width is the Gaussian fraction of its tangent width is Gaussian. A
    tailing peak is not: its tangent width grows while the half-height width barely moves, so the
    half-height plate count stays high while the real efficiency has dropped. Here the same peak
    reads 39% more efficient under the convention that does not see the tail.
    """
    retention = 6.0
    half_height_width = HALF_HEIGHT_PER_SIGMA * 0.05
    tailed_tangent_width = TANGENT_PER_SIGMA * 0.05 * 1.18  # the tail drags the baseline out
    optimistic = peaks.plate_count(retention, half_height_width, "half_height")
    honest = peaks.plate_count(retention, tailed_tangent_width, "tangent")
    assert optimistic.plates > honest.plates
    assert optimistic.plates / honest.plates == pytest.approx(1.39, abs=0.01)


def test_a_symmetric_peak_has_a_tailing_factor_of_exactly_one() -> None:
    """T = W/(2f) with f = W/2 is 1 by construction, so this is exact rather than approximate."""
    result = peaks.symmetry_factor(width_at_5_percent_min=0.40, leading_half_width_min=0.20)
    assert result.tailing_factor == 1.0
    assert result.shape == "symmetric"


def test_a_fronting_peak_is_reported_as_fronting_rather_than_as_nearly_symmetric() -> None:
    """T < 1 is a different fault from T > 1, and folding them into |T-1| would hide it.

    Tailing is usually a secondary interaction or an analyte near its pKa; fronting is usually
    column overload or too strong an injection solvent. A caller thresholding the distance from 1
    would prescribe the wrong fix.
    """
    fronting = peaks.symmetry_factor(width_at_5_percent_min=0.40, leading_half_width_min=0.25)
    tailing = peaks.symmetry_factor(width_at_5_percent_min=0.40, leading_half_width_min=0.15)
    assert fronting.shape == "fronting"
    assert fronting.tailing_factor < 1.0
    assert tailing.shape == "tailing"
    assert tailing.tailing_factor > 1.0


def test_a_leading_half_wider_than_the_whole_peak_is_refused_by_name() -> None:
    """The realistic mistake: measuring `f` across the peak instead of to the maximum.

    Computed anyway it gives T < 0.5, which reads as a badly fronting peak rather than as a
    mis-measurement, so the wrong fix gets prescribed for a peak that may be fine.
    """
    with pytest.raises(peaks.PeakError, match="wider than the whole peak"):
        peaks.symmetry_factor(width_at_5_percent_min=0.40, leading_half_width_min=0.45)


def test_peaks_given_in_the_wrong_order_are_refused_rather_than_signed() -> None:
    """Rs is defined between an earlier and a later peak; reversed, it would come back negative."""
    with pytest.raises(peaks.PeakError, match="not after"):
        peaks.resolution(5.4, 5.0, 0.2, 0.2, "tangent")


def test_the_retention_factor_names_an_unretained_peak_rather_than_returning_a_small_number() -> (
    None
):
    """k <= 0 means the peak is at or before the void: not retained, not being separated."""
    retained = peaks.retention_factor(retention_time_min=6.0, void_time_min=1.2)
    assert retained.retention_factor == pytest.approx(4.0)
    assert retained.unretained is False

    at_the_void = peaks.retention_factor(retention_time_min=1.2, void_time_min=1.2)
    assert at_the_void.retention_factor == 0.0
    assert at_the_void.unretained is True


def test_the_separation_factor_refuses_an_unretained_peak_instead_of_dividing_by_it() -> None:
    """alpha = k2/k1 with k1 = 0 is undefined; computed anyway it is infinite or negative.

    Either would arrive looking like a selectivity, which is the shape this fleet's "refuse rather
    than approximate" rule exists for.
    """
    unretained = peaks.retention_factor(retention_time_min=1.2, void_time_min=1.2)
    retained = peaks.retention_factor(retention_time_min=6.0, void_time_min=1.2)
    with pytest.raises(peaks.PeakError, match="both peaks retained"):
        peaks.separation_factor(unretained, retained)


def test_the_separation_factor_is_one_when_two_peaks_co_elute() -> None:
    """The boundary that says the number means what it should: no selectivity is alpha = 1."""
    first = peaks.retention_factor(retention_time_min=6.0, void_time_min=1.2)
    second = peaks.retention_factor(retention_time_min=6.0, void_time_min=1.2)
    assert peaks.separation_factor(first, second) == pytest.approx(1.0)


def test_plates_per_metre_is_absent_rather_than_zero_when_no_column_length_was_given() -> None:
    """`None` and `0.0` are different facts, and a zero would be read as a measurement."""
    without = peaks.plate_count(6.0, 0.2, "tangent")
    assert without.plates_per_metre is None
    with_length = peaks.plate_count(6.0, 0.2, "tangent", column_length_mm=150.0)
    assert with_length.plates_per_metre == pytest.approx(without.plates * 1000.0 / 150.0)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"retention_time_min": 0.0, "width_min": 0.2}, "retention time"),
        ({"retention_time_min": 6.0, "width_min": 0.0}, "peak width"),
        ({"retention_time_min": 6.0, "width_min": -0.2}, "peak width"),
    ],
)
def test_a_non_positive_measurement_is_refused_naming_which_one(
    kwargs: dict[str, float], match: str
) -> None:
    """Each refusal names the measurement, because "invalid input" does not tell a chemist which."""
    with pytest.raises(peaks.PeakError, match=match):
        peaks.plate_count(convention="tangent", **kwargs)
