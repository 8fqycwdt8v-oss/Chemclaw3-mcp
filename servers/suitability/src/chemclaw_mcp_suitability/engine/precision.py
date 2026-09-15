"""Replicate-injection precision, and the USP <621> rule for how many injections that needs.

The arithmetic is one line — RSD = 100 s / mean, with `s` the **sample** standard deviation over
n-1 degrees of freedom, which is what every chromatography data system reports and what the
acceptance criterion is written against. Using the population form over n would understate the RSD
by 8.7% at n = 6, which is the difference between passing and failing a 2.0% limit at a true 2.1%.

The part worth a module is the rule attached to it. USP <621> *System Suitability* says, for the
repeatability of an assay:

    data from **five** replicate injections are used to calculate the relative standard deviation
    if the requirement is 2.0% or less; data from **six** replicate injections are used if the
    requirement is more than 2.0%.

That reads backwards to most people the first time — a *tighter* limit needs *fewer* injections —
and it is not arbitrary: the required count comes from the confidence with which a sample RSD
bounds the true one, and a looser limit is being asked to catch a larger true variability, which
takes more degrees of freedom to see. Whatever the reasoning, it is a rule with a number in it,
the chemist is the one who has to remember which way round it goes, and the data system does not
check it. So this module answers "is this many injections enough for this limit?" beside the RSD
itself, and says which branch of the rule applied.

**What this is not**: it is not intermediate precision, not reproducibility, and not a validation
exercise. Six injections of one solution on one instrument on one day bound the *system's*
repeatability at the moment of the run, and nothing else. A method's precision is an ICH Q2
exercise over separate preparations, and it is deliberately not here.
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

#: Below two values there is no sample standard deviation to compute — n-1 is zero, and the
#: expression is undefined rather than large. USP never asks for fewer than five; this is the
#: floor at which the arithmetic exists at all, and the rule check is what enforces USP's count.
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
    #: The limit the caller declared, in percent, or `None` if they declared none. When `None`,
    #: `meets_limit` and `injections_are_sufficient` are `None` too: this module will not invent a
    #: limit, because 2.0% is an assay's convention and a related-substances method's is not.
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
        PrecisionError: If the limit is not positive. A limit of zero or below cannot be met by
            any real series and is far more likely to be a units mistake — 0.02 entered as a
            fraction where percent was meant — than a deliberate criterion.
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
        values: The measured responses — peak areas, or retention times, one per injection. Their
            unit does not matter and is not asked for: an RSD is a ratio, so it is the same number
            whether the areas are counts or mAU*s. They must all be measurements of the same
            thing, which this cannot check.
        limit_percent: The acceptance limit in percent, if there is one. Given, the answer says
            whether the series meets it and whether USP <621> accepts this many injections for a
            limit of that size. Omitted, the RSD is returned alone with no verdict attached.

    Returns:
        The RSD, the mean, the sample standard deviation, and — when a limit was declared — the
        two verdicts.

    Raises:
        PrecisionError: If fewer than two values are given, if any is not finite, or if the mean
            is zero. A zero mean makes the RSD undefined rather than infinite, and a series
            averaging zero is a baseline or a sign error rather than a peak.
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
    # The RSD is conventionally reported as a positive percentage even where the mean is negative
    # (a refractive-index or a subtracted-baseline detector can give one), so the magnitude of the
    # mean is what it is taken over. A negative RSD would be read as an error rather than a spread.
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
