"""Per-peak chromatographic metrics: plate count, tailing, resolution, retention.

All formulas are USP General Chapter <621>, each stated with its width convention, since the
conventions disagree on a real peak:

    N = 16 (t_R / W)^2                              tangent width
    N = 5.54 (t_R / W_0.5)^2                        width at half height
    Rs = 2 (t_R2 - t_R1) / (W_1 + W_2)              tangent widths
    Rs = 1.18 (t_R2 - t_R1) / (W_0.5,1 + W_0.5,2)   half-height widths
    T = W_0.05 / (2 f)                              tailing at 5% height

The forms agree only for a Gaussian peak; the half-height resolution substitutes the Gaussian width
relation and is optimistic on tailing peaks. Each answer names its convention and nothing converts
between them. T < 1 (fronting) is reported, not treated as symmetric. Nothing here integrates a
chromatogram: inputs are numbers already reported, taken as given.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "GAUSSIAN_HALF_HEIGHT_FACTOR",
    "PeakError",
    "PlateCount",
    "Resolution",
    "RetentionFactor",
    "Symmetry",
    "WidthConvention",
    "plate_count",
    "resolution",
    "retention_factor",
    "separation_factor",
    "symmetry_factor",
]

#: 2 sqrt(2 ln 2) / 2, relating a Gaussian's half-height width to its tangent width; USP <621>
#: writes it 1.18.
GAUSSIAN_HALF_HEIGHT_FACTOR = 1.18

#: Which width a caller measured. No default: the conventions give different numbers on one peak.
WidthConvention = Literal["tangent", "half_height"]


class PeakError(ValueError):
    """A peak measurement that cannot be interpreted as written.

    A `ValueError` so `mcp_server_kit` passes the chemist-facing message to the model verbatim.
    """


def _positive(value: float, what: str) -> float:
    """A width, a time or a height that must be finite and greater than zero to mean anything.

    Finite first: `value <= 0.0` is False for NaN and infinity, which JSON input can carry.
    """
    if not math.isfinite(value):
        raise PeakError(f"{what} must be a finite number; got {value}.")
    if value <= 0.0:
        raise PeakError(
            f"{what} must be greater than zero; got {value}. A width or a retention time of zero "
            "is not a measurement this can use."
        )
    return value


@dataclass(frozen=True)
class PlateCount:
    """A column efficiency, with the convention that produced it.

    The convention is part of the answer: the two forms agree only for a Gaussian peak.
    """

    plates: float
    convention: WidthConvention
    retention_time_min: float
    width_min: float
    #: Plates per metre when a column length was given; `None` (not zero) otherwise.
    plates_per_metre: float | None


@dataclass(frozen=True)
class Symmetry:
    """A USP tailing factor at 5% height, and which way the peak is skewed."""

    tailing_factor: float
    #: "tailing" (T > 1), "fronting" (T < 1) or "symmetric" (T == 1 exactly); the two faults have
    #: different causes.
    shape: Literal["tailing", "fronting", "symmetric"]
    width_at_5_percent_min: float
    leading_half_width_min: float


@dataclass(frozen=True)
class Resolution:
    """The separation between two adjacent peaks, with its convention."""

    resolution: float
    convention: WidthConvention
    first_retention_min: float
    second_retention_min: float
    #: True when half-height widths were used, so the answer rests on the Gaussian 1.18 relation.
    assumes_gaussian: bool


@dataclass(frozen=True)
class RetentionFactor:
    """k = (t_R - t_0)/t_0 — how long a solute is retained relative to an unretained one."""

    retention_factor: float
    retention_time_min: float
    void_time_min: float
    #: True when k <= 0: the compound is not retained and nothing is being separated.
    unretained: bool


def plate_count(
    retention_time_min: float,
    width_min: float,
    convention: WidthConvention,
    column_length_mm: float | None = None,
) -> PlateCount:
    """Column efficiency from one peak, by whichever USP <621> form matches the width given.

    Args:
        retention_time_min: The peak's retention time, in minutes.
        width_min: The peak width, in minutes, measured by `convention`.
        convention: `"tangent"` for the baseline width between the tangents (N = 16 (t_R/W)^2) or
            `"half_height"` for the width at half height (N = 5.54 (t_R/W_0.5)^2).
        column_length_mm: The column length in millimetres, if plates per metre are wanted.

    Returns:
        The plate count, the convention it was computed under, and plates per metre when a column
        length was given.

    Raises:
        PeakError: If a time or width is not positive, or the column length is not positive.
    """
    _positive(retention_time_min, "the retention time")
    _positive(width_min, "the peak width")
    ratio = retention_time_min / width_min
    plates = (16.0 if convention == "tangent" else 5.54) * ratio * ratio
    per_metre: float | None = None
    if column_length_mm is not None:
        _positive(column_length_mm, "the column length")
        per_metre = plates * 1000.0 / column_length_mm
    return PlateCount(
        plates=plates,
        convention=convention,
        retention_time_min=retention_time_min,
        width_min=width_min,
        plates_per_metre=per_metre,
    )


def symmetry_factor(
    width_at_5_percent_min: float,
    leading_half_width_min: float,
) -> Symmetry:
    """The USP tailing factor, T = W_0.05 / (2 f), measured at 5% of peak height.

    Args:
        width_at_5_percent_min: The full peak width at 5% of peak height, in minutes.
        leading_half_width_min: `f`, the distance in minutes from the leading edge of the peak at
            5% height to the peak maximum — the *front* half of that width, not the rear.

    Returns:
        The tailing factor and which way the peak is skewed.

    Raises:
        PeakError: If either measurement is not positive, or if the leading half is wider than
            the whole width it is supposed to be part of.
    """
    _positive(width_at_5_percent_min, "the width at 5% height")
    _positive(leading_half_width_min, "the leading half-width `f`")
    if leading_half_width_min > width_at_5_percent_min:
        raise PeakError(
            f"the leading half-width `f` ({leading_half_width_min} min) is wider than the whole "
            f"peak at 5% height ({width_at_5_percent_min} min), so one of the two is not what it "
            "is labelled. `f` is measured from the leading edge to the peak *maximum*, not across "
            "the peak."
        )
    tailing = width_at_5_percent_min / (2.0 * leading_half_width_min)
    if tailing > 1.0:
        shape: Literal["tailing", "fronting", "symmetric"] = "tailing"
    elif tailing < 1.0:
        shape = "fronting"
    else:
        shape = "symmetric"
    return Symmetry(
        tailing_factor=tailing,
        shape=shape,
        width_at_5_percent_min=width_at_5_percent_min,
        leading_half_width_min=leading_half_width_min,
    )


def resolution(
    first_retention_min: float,
    second_retention_min: float,
    first_width_min: float,
    second_width_min: float,
    convention: WidthConvention,
) -> Resolution:
    """Resolution between two adjacent peaks, by the USP <621> form matching the widths given.

    Args:
        first_retention_min: Retention time of the earlier peak, in minutes.
        second_retention_min: Retention time of the later peak, in minutes.
        first_width_min: The earlier peak's width in minutes, by `convention`.
        second_width_min: The later peak's width in minutes, by `convention`.
        convention: `"tangent"` for Rs = 2 dt / (W1 + W2), or `"half_height"` for
            Rs = 1.18 dt / (W_0.5,1 + W_0.5,2). The half-height form substitutes the Gaussian
            relation between the two widths, so it is an approximation on a peak that tails.

    Returns:
        The resolution, its convention, and whether it rests on the Gaussian assumption.

    Raises:
        PeakError: If a width is not positive, or the two peaks are given in the wrong order, or
            they have the same retention time.
    """
    _positive(first_width_min, "the first peak's width")
    _positive(second_width_min, "the second peak's width")
    _positive(first_retention_min, "the first peak's retention time")
    _positive(second_retention_min, "the second peak's retention time")
    if second_retention_min <= first_retention_min:
        raise PeakError(
            f"the second peak's retention time ({second_retention_min} min) is not after the "
            f"first's ({first_retention_min} min). Resolution is defined between an earlier and a "
            "later peak; swap them, or check which pair was meant."
        )
    separation = second_retention_min - first_retention_min
    total_width = first_width_min + second_width_min
    numerator = 2.0 if convention == "tangent" else GAUSSIAN_HALF_HEIGHT_FACTOR
    return Resolution(
        resolution=numerator * separation / total_width,
        convention=convention,
        first_retention_min=first_retention_min,
        second_retention_min=second_retention_min,
        assumes_gaussian=convention == "half_height",
    )


def retention_factor(retention_time_min: float, void_time_min: float) -> RetentionFactor:
    """k = (t_R - t_0) / t_0, the retention relative to an unretained marker.

    Args:
        retention_time_min: The peak's retention time, in minutes.
        void_time_min: `t_0`, the column void (hold-up) time in minutes — the elution time of an
            unretained marker, not the dwell volume and not the gradient delay.

    Returns:
        The retention factor, and whether the peak is unretained (k <= 0).

    Raises:
        PeakError: If either time is not positive.
    """
    _positive(retention_time_min, "the retention time")
    _positive(void_time_min, "the void time `t_0`")
    factor = (retention_time_min - void_time_min) / void_time_min
    return RetentionFactor(
        retention_factor=factor,
        retention_time_min=retention_time_min,
        void_time_min=void_time_min,
        unretained=factor <= 0.0,
    )


def separation_factor(first: RetentionFactor, second: RetentionFactor) -> float:
    """alpha = k2 / k1 for two peaks, the later over the earlier.

    Args:
        first: The earlier-eluting peak's retention factor.
        second: The later-eluting peak's retention factor.

    Returns:
        The separation factor: 1.0 when the two co-elute, greater otherwise.

    Raises:
        PeakError: Either peak is unretained, since alpha is undefined when k1 <= 0.
    """
    if first.unretained or second.unretained:
        raise PeakError(
            "the separation factor needs both peaks retained (k > 0), and "
            f"k1 = {first.retention_factor:.4g}, k2 = {second.retention_factor:.4g}. A peak at or "
            "before the void time is not being separated from anything, so there is no "
            "selectivity to report."
        )
    return second.retention_factor / first.retention_factor
