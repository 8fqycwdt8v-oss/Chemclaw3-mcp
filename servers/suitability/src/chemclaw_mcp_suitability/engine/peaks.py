"""Per-peak chromatographic metrics: plate count, tailing, resolution, retention.

Every formula here is USP General Chapter <621> *Chromatography*, and each is stated with the
**width convention it is defined over**, because that is the part a chemist gets wrong and a
number carries no label. USP defines the plate count two ways that disagree on a real peak:

    N = 16 (t_R / W)^2          tangent width, the baseline intercepts of the tangents
    N = 5.54 (t_R / W_0.5)^2    width at half height

They agree exactly for a Gaussian peak and diverge as it tails, so a plate count quoted without
its convention cannot be compared with a limit quoted with one. Both are offered and each answer
says which it used; nothing converts between them, because the conversion is only valid under the
Gaussian assumption the divergence already violates.

The same split governs resolution:

    Rs = 2 (t_R2 - t_R1) / (W_1 + W_2)        tangent widths
    Rs = 1.18 (t_R2 - t_R1) / (W_0.5,1 + W_0.5,2)   half-height widths

where 1.18 is 2 sqrt(2 ln 2) / 2 — the half-height form is the tangent form with the Gaussian
relation W = W_0.5 / 1.18 substituted in, so it is an *approximation* on a tailing peak while the
tangent form is a definition. Chemists use the half-height form because a data system reports it
without asking, and it is the optimistic one on exactly the peaks where resolution is in doubt.

The tailing (symmetry) factor is measured at 5% of peak height:

    T = W_0.05 / (2 f)

with `W_0.05` the full width at 5% height and `f` the distance from the leading edge to the peak
maximum at that height. T = 1 is symmetric; T > 1 is tailing; T < 1 is fronting, which this module
reports rather than treats as an error, because a fronting peak is a real and different fault
(overload or a mismatched injection solvent) and rounding it to "symmetric enough" hides it.

**Nothing here integrates a chromatogram.** Every input is a number a chemist or a data system
already reported. This module cannot find a peak, assign a baseline or resolve a shoulder, and a
retention time it is handed is taken as given.
"""

from __future__ import annotations

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

#: 2 sqrt(2 ln 2) / 2 — relates a Gaussian peak's half-height width to its tangent width, and is
#: the whole of the difference between the two resolution forms below. USP <621> writes it 1.18.
GAUSSIAN_HALF_HEIGHT_FACTOR = 1.18

#: Which width a caller measured. There is no default: the two conventions give different numbers
#: on the same peak, and guessing one would publish a plate count nobody can compare to a limit.
WidthConvention = Literal["tangent", "half_height"]


class PeakError(ValueError):
    """A peak measurement that cannot be interpreted as written.

    `ValueError` deliberately: `mcp_server_kit` lets this family through to the model verbatim, so
    the message is written for a chemist reading it in a chat rather than for a log.
    """


def _positive(value: float, what: str) -> float:
    """A width, a time or a height that must be greater than zero to mean anything."""
    if value <= 0.0:
        raise PeakError(
            f"{what} must be greater than zero; got {value}. A width or a retention time of zero "
            "is not a measurement this can use."
        )
    return value


@dataclass(frozen=True)
class PlateCount:
    """A column efficiency, with the convention that produced it.

    `convention` is part of the answer rather than metadata: 16 (t_R/W)^2 and 5.54 (t_R/W_0.5)^2
    are the same number only for a Gaussian peak, so a limit and a measurement must agree on
    which was meant before they can be compared.
    """

    plates: float
    convention: WidthConvention
    retention_time_min: float
    width_min: float
    #: Plates per metre, when the caller said how long the column is. `None` when they did not —
    #: not zero, because "nobody said" and "a zero-length column" are different facts.
    plates_per_metre: float | None


@dataclass(frozen=True)
class Symmetry:
    """A USP tailing factor at 5% height, and which way the peak is skewed."""

    tailing_factor: float
    #: "tailing" (T > 1), "fronting" (T < 1) or "symmetric" (T == 1 exactly). Reported rather
    #: than folded into the number, because a fronting peak has a different cause from a tailing
    #: one and a caller comparing |T - 1| to a threshold would treat them as one fault.
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
    #: True when the caller's widths were half-height ones, so the answer used the 1.18 Gaussian
    #: relation and is an approximation on a peak that is not Gaussian.
    assumes_gaussian: bool


@dataclass(frozen=True)
class RetentionFactor:
    """k = (t_R - t_0)/t_0 — how long a solute is retained relative to an unretained one."""

    retention_factor: float
    retention_time_min: float
    void_time_min: float
    #: True when the peak elutes at or before the void time, which means k <= 0: the compound is
    #: not retained and the method is not separating it from anything.
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
        The separation factor, which is 1.0 when the two co-elute and greater otherwise.

    Raises:
        PeakError: If either peak is unretained, since alpha is undefined when k1 <= 0 — and a
            ratio computed anyway would be negative or infinite while looking like a selectivity.
    """
    if first.unretained or second.unretained:
        raise PeakError(
            "the separation factor needs both peaks retained (k > 0), and "
            f"k1 = {first.retention_factor:.4g}, k2 = {second.retention_factor:.4g}. A peak at or "
            "before the void time is not being separated from anything, so there is no "
            "selectivity to report."
        )
    return second.retention_factor / first.retention_factor
