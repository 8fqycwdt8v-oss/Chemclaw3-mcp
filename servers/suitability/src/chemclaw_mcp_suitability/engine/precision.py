"""Replicate-injection precision, and the USP <621> rule for how many injections that needs.

RSD = 100 s / mean, with `s` the sample standard deviation over n-1, as data systems report and
acceptance criteria assume (the population form understates it by 8.7% at n = 6). USP <621> requires
five replicate injections for a limit of 2.0% or less and six above it — a tighter limit takes fewer
— and data systems do not check it, so this module reports which branch applied beside the RSD.

This is system repeatability for one run only, not intermediate precision or an ICH Q2 validation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "MINIMUM_INJECTIONS",
    "PrecisionError",
    "ReplicatePrecision",
    "injections_required_for",
    "relative_standard_deviation",
]

#: Below two values the sample standard deviation is undefined; this is the arithmetic floor, while
#: the rule check enforces USP's count.
MINIMUM_INJECTIONS = 2

#: The limit, in percent, at or below which USP <621> accepts five injections. Above it, six.
_FIVE_INJECTION_LIMIT_PERCENT = 2.0


class PrecisionError(ValueError):
    """A replicate series that cannot be interpreted as written."""


@dataclass(frozen=True)
class ReplicatePrecision:
    """The precision of a replicate injection series, against the rule that governs its size."""

    relative_standard_deviation_percent: float
    mean: float
    #: The sample standard deviation, over n-1 — the same denominator the acceptance criterion and
    #: every data system use.
    standard_deviation: float
    injections: int
    #: The declared limit in percent, or `None`, in which case both verdicts are `None` too: no
    #: default is invented, since 2.0% is only an assay convention.
    limit_percent: float | None
    meets_limit: bool | None
    #: How many injections USP <621> requires *for that limit*, or `None` with no limit declared.
    injections_required: int | None
    injections_are_sufficient: bool | None


def injections_required_for(limit_percent: float) -> int:
    """How many replicate injections USP <621> requires for a given RSD limit.

    Args:
        limit_percent: The RSD acceptance limit, in percent.

    Returns:
        Five if the limit is 2.0% or less, six if it is more.

    Raises:
        PrecisionError: The limit is not positive (most likely a fraction entered where percent was
        meant).
    """
    if limit_percent <= 0.0:
        raise PrecisionError(
            f"an RSD limit must be greater than zero percent; got {limit_percent}. If this was "
            "meant as a fraction, the limit here is in percent: 2.0 means 2.0%, not 200%."
        )
    return 5 if limit_percent <= _FIVE_INJECTION_LIMIT_PERCENT else 6


def relative_standard_deviation(
    values: list[float],
    limit_percent: float | None = None,
) -> ReplicatePrecision:
    """RSD over a replicate injection series, and whether the series is big enough for its limit.

    Args:
        values: The measured responses (peak areas or retention times), one per injection, all of
        the same thing. Unit-free, since an RSD is a ratio. limit_percent: The acceptance limit in
        percent, if any. Given, the answer says whether the series meets it and whether USP <621>
        accepts this many injections; omitted, the RSD comes alone.

    Returns:
        The RSD, the mean, the sample standard deviation, and the two verdicts when a limit was
        declared.

    Raises:
        PrecisionError: Fewer than two values, a non-finite value, or a zero mean (RSD undefined).
    """
    if len(values) < MINIMUM_INJECTIONS:
        raise PrecisionError(
            f"a relative standard deviation needs at least {MINIMUM_INJECTIONS} injections; got "
            f"{len(values)}. With one value there is no spread to measure."
        )
    for index, value in enumerate(values, start=1):
        if not math.isfinite(value):
            raise PrecisionError(
                f"injection {index} is {value}, which is not a finite number. Every value must be "
                "a measured response."
            )
    count = len(values)
    mean = math.fsum(values) / count
    if mean == 0.0:
        raise PrecisionError(
            "the mean response is zero, so the relative standard deviation is undefined rather "
            "than large. A replicate series averaging zero is a baseline or a sign error, not a "
            "peak."
        )
    variance = math.fsum((value - mean) ** 2 for value in values) / (count - 1)
    deviation = math.sqrt(variance)
    # Taken over |mean| so the RSD stays a positive spread even for a detector giving negative
    # responses.
    rsd = 100.0 * deviation / abs(mean)

    meets: bool | None = None
    required: int | None = None
    sufficient: bool | None = None
    if limit_percent is not None:
        required = injections_required_for(limit_percent)
        meets = rsd <= limit_percent
        sufficient = count >= required

    return ReplicatePrecision(
        relative_standard_deviation_percent=rsd,
        mean=mean,
        standard_deviation=deviation,
        injections=count,
        limit_percent=limit_percent,
        meets_limit=meets,
        injections_required=required,
        injections_are_sufficient=sufficient,
    )
