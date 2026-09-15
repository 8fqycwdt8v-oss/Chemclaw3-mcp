"""The `suitability` MCP tool surface: USP <621> system-suitability arithmetic.

**These docstrings are the prompt**, and the thing they have to get across on this server is a
distinction the numbers themselves do not carry: *which convention a measurement was made under*.
A plate count of 14,400 and a plate count of 14,387 are the same peak measured two ways, and a
resolution of 2.00 and 2.01 likewise — but a plate count quoted without its convention cannot be
compared with a limit quoted with one, and on a tailing peak the two diverge in the direction that
makes a marginal method look acceptable. So `convention` is a required argument with no default on
every tool where USP defines two forms, and every answer says which it used.

**Nothing here integrates a chromatogram.** Every input is a number a chemist or a data system
already reported: a retention time, a width, an area. This server cannot find a peak, assign a
baseline, resolve a shoulder or open an instrument file, and a retention time it is handed is taken
as given. That is the boundary worth stating to a model, because "system suitability" sounds like
something done *to* a chromatogram and this is the arithmetic done *after* one.

**What it is not evidence of.** Passing system suitability says the instrument and column performed
acceptably at the moment of the run. It says nothing about whether the method is fit for its
purpose — that is an ICH Q2 validation exercise over separate preparations, deliberately not
served here — and nothing about whether the result is accurate. A method can pass every suitability
criterion in its monograph and still be measuring the wrong peak.

Every answer carries `basis`: the formula it came out of and the convention or assumption behind
it. A resolution from half-height widths rests on a Gaussian assumption that a tailing peak already
violates; a result that does not say so cannot be put in a report.

The tools are synchronous and closed-form. Measured rather than guessed, and the measurement
corrected the guess that preceded it in this paragraph: the slowest is `system_suitability_report`
at **29.8 µs** for a three-peak table with two six-injection replicate series, and the six
primitives are **1.9 µs to 4.9 µs** — not the 10.6 µs and 0.3-1.4 µs first written here, which were
recalled from the shape of the arithmetic rather than timed. Most of each figure is pydantic
building the result model, not the chromatography. No corpus is loaded at call time, no subprocess
is forked and no thread is pinned, so there is nothing here for an admission ceiling to bound —
`servers/calc` has one because a single call there is minutes of CPU across forked workers. A tool
added here that grows real work must revisit that.
"""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from chemclaw_mcp_suitability.engine import adjustments, peaks, precision

server = FastMCP("suitability")

#: The most replicate injections `replicate_precision` will take in one call. A bound on the input
#: so the cost cannot run away unpriced, which `docs/adding-a-server.md` asks of a slow tool and
#: which applies to a fast one whose input length is unbounded. A suitability series is five or
#: six injections and a long stability sequence is tens; 1,000 refuses a payload while refusing no
#: real run.
MAX_INJECTIONS = 1000

#: The most peaks `system_suitability_report` will accept. A related-substances chromatogram may
#: legitimately report dozens of peaks; beyond this it is not a suitability table.
MAX_PEAKS = 200


class ReplicatePrecisionResult(BaseModel):
    """Precision over a replicate injection series, against the rule that governs its size."""

    relative_standard_deviation_percent: float = Field(
        description="RSD as a percentage, over the sample standard deviation (n-1 denominator)."
    )
    mean: float = Field(description="Mean response, in whatever unit the values were given in.")
    standard_deviation: float = Field(
        description="Sample standard deviation (n-1), in the values' own unit."
    )
    injections: int = Field(description="How many values were supplied.")
    meets_limit: bool | None = Field(
        description="Whether the RSD is at or below the declared limit; null if none was declared."
    )
    injections_required: int | None = Field(
        description=(
            "How many replicate injections USP <621> requires for a limit of that size — five at "
            "2.0% or below, six above it. Null if no limit was declared."
        )
    )
    injections_are_sufficient: bool | None = Field(
        description=(
            "Whether enough injections were supplied for the declared limit. Null if no limit was "
            "declared. A series that meets its limit on too few injections has not demonstrated "
            "precision, and this is the half a data system does not check."
        )
    )
    basis: str


class PlateCountResult(BaseModel):
    """A column efficiency, with the convention that produced it."""

    plates: float = Field(description="Theoretical plate count, dimensionless.")
    convention: str = Field(description="'tangent' (N = 16(t/W)^2) or 'half_height' (N = 5.54...).")
    plates_per_metre: float | None = Field(
        description="Plates per metre, if a column length was given; null if it was not."
    )
    basis: str


class SymmetryResult(BaseModel):
    """A USP tailing factor at 5% of peak height."""

    tailing_factor: float = Field(
        description="T = W_0.05 / 2f. 1.0 is symmetric, above 1 tails, below 1 fronts."
    )
    shape: str = Field(description="'tailing', 'fronting' or 'symmetric'.")
    basis: str


class ResolutionResult(BaseModel):
    """The separation between two adjacent peaks, with its convention."""

    resolution: float = Field(description="Rs, dimensionless.")
    convention: str = Field(description="'tangent' or 'half_height'.")
    assumes_gaussian: bool = Field(
        description=(
            "True when half-height widths were used, so the 1.18 factor's Gaussian relation is "
            "assumed — an approximation on a peak that tails, and the optimistic one."
        )
    )
    basis: str


class RetentionResult(BaseModel):
    """Retention factor for one peak, and selectivity against a second if one was given."""

    retention_factor: float = Field(description="k = (t_R - t_0)/t_0, dimensionless.")
    unretained: bool = Field(
        description="True when k <= 0: the peak elutes at or before the void time."
    )
    second_retention_factor: float | None = Field(
        description="k for the second peak, if one was given; null otherwise."
    )
    separation_factor: float | None = Field(
        description="alpha = k2/k1, if a second peak was given; null otherwise."
    )
    basis: str


class AdjustmentResult(BaseModel):
    """Whether a proposed method change is within what USP <621> permits."""

    permitted: bool = Field(
        description="True if the change is within the general chapter's allowance."
    )
    reason: str = Field(description="Why, in words, naming which bound applied.")
    limit_value: float | None = Field(
        description=(
            "The furthest value the allowance reaches in the direction asked for; null where the "
            "allowance is not a number."
        )
    )
    requested_relative_percent: float | None = Field(
        description="The change asked for as a percentage of the original; null if it was zero."
    )
    basis: str


class PeakReport(BaseModel):
    """One peak's suitability metrics inside a whole-run report."""

    name: str
    retention_time_min: float
    plates: float | None = Field(description="Null if no width was given for this peak.")
    tailing_factor: float | None = Field(description="Null if no 5% measurements were given.")
    resolution_from_previous: float | None = Field(
        description="Rs against the peak before it; null for the first peak or with no widths."
    )


class SuitabilityReport(BaseModel):
    """A whole injection sequence against a method's declared criteria."""

    area_precision: ReplicatePrecisionResult | None = Field(
        description="Precision over the replicate areas; null if fewer than two were given."
    )
    retention_precision: ReplicatePrecisionResult | None = Field(
        description="Precision over the replicate retention times; null if fewer than two."
    )
    peaks: list[PeakReport]
    failures: list[str] = Field(
        description=(
            "Every declared criterion this run does not meet, in words. Empty means every "
            "criterion that was *declared* passed — not that the run is suitable, since a "
            "criterion nobody declared cannot be checked."
        )
    )
    basis: str


_PLATE_BASIS = {
    "tangent": "USP <621> N = 16 (t_R/W)^2, tangent (baseline) width",
    "half_height": "USP <621> N = 5.54 (t_R/W_0.5)^2, width at half height",
}
_RESOLUTION_BASIS = {
    "tangent": "USP <621> Rs = 2(t2-t1)/(W1+W2), tangent widths",
    "half_height": (
        "USP <621> Rs = 1.18(t2-t1)/(W0.5,1+W0.5,2), half-height widths — the 1.18 is the "
        "Gaussian relation between the two widths, so this form approximates on a tailing peak"
    ),
}


def _precision_result(values: list[float], limit: float | None) -> ReplicatePrecisionResult:
    """Bound the series, compute it, and shape the answer.

    Extracted because `system_suitability_report` needs the same result and `@server.tool()`
    returns the *undecorated* function typed as `Any` — so a report that called the tool would be
    building its own half of the answer untyped, which `mypy --strict` catches and which would
    otherwise be the first place the two paths could drift apart.
    """
    _check_series(values, "injections")
    measured = precision.relative_standard_deviation(values, limit)
    return ReplicatePrecisionResult(
        relative_standard_deviation_percent=measured.relative_standard_deviation_percent,
        mean=measured.mean,
        standard_deviation=measured.standard_deviation,
        injections=measured.injections,
        meets_limit=measured.meets_limit,
        injections_required=measured.injections_required,
        injections_are_sufficient=measured.injections_are_sufficient,
        basis=(
            "RSD = 100 s / |mean| with s the sample standard deviation over n-1; the injection "
            "count rule is USP <621> System Suitability (five injections at a limit of 2.0% or "
            "below, six above it)"
        ),
    )


def _check_series(values: list[float], what: str) -> None:
    """Refuse a series longer than this server will price."""
    if len(values) > MAX_INJECTIONS:
        raise ValueError(
            f"{len(values)} {what} is more than this tool will take in one call "
            f"({MAX_INJECTIONS}). A suitability series is five or six injections; if this is a "
            "whole sequence, send the replicate set that the criterion is written against."
        )


@server.tool()
def replicate_precision(
    values: Annotated[
        list[float],
        Field(
            min_length=2,
            description=(
                "The measured responses, one per injection — peak areas, or retention times in "
                "minutes. Their unit does not matter and is not asked for, since an RSD is a "
                "ratio; they must all measure the same thing."
            ),
        ),
    ],
    limit_percent: Annotated[
        float | None,
        Field(
            default=None,
            description=(
                "The RSD acceptance limit in percent, if the method declares one. Given, the "
                "answer says whether the series meets it AND whether USP <621> accepts this many "
                "injections for a limit of that size. Omitted, the RSD comes back with no verdict."
            ),
        ),
    ] = None,
) -> ReplicatePrecisionResult:
    """Relative standard deviation over a replicate injection series, with the <621> count rule.

    The RSD uses the **sample** standard deviation (n-1 denominator), which is what every
    chromatography data system reports and what an acceptance criterion is written against. The
    population form would understate it by 8.7% at six injections — the difference between passing
    and failing a 2.0% limit at a true 2.1%.

    Beside the number it applies the rule that is easy to get backwards: USP <621> accepts **five**
    replicate injections where the requirement is 2.0% or less, and requires **six** where it is
    more. A tighter limit takes fewer injections, not more. A series that meets its limit on too
    few injections has not demonstrated precision, and no data system checks this.

    This is *system* repeatability at the moment of the run: one solution, one instrument, one
    sequence. It is not intermediate precision, not reproducibility, and not evidence that the
    method is precise — those are ICH Q2 exercises over separate preparations.

    Args:
        values: The measured responses, one per injection.
        limit_percent: The acceptance limit in percent, if there is one.

    Returns:
        The RSD, mean, sample standard deviation, and — with a limit declared — whether it is met
        and whether the series is large enough for a limit of that size.

    Raises:
        ValueError: If fewer than two values are given, if any is not finite, if the mean is zero
            (an RSD is undefined rather than large), or if more values are sent than this tool
            prices.
    """
    return _precision_result(values, limit_percent)


@server.tool()
def plate_count(
    retention_time_min: Annotated[float, Field(gt=0, description="Retention time, in minutes.")],
    width_min: Annotated[
        float, Field(gt=0, description="Peak width in minutes, measured by `convention`.")
    ],
    convention: Annotated[
        Literal["tangent", "half_height"],
        Field(
            description=(
                "Which width was measured. 'tangent' is the baseline width between the tangents "
                "(N = 16(t/W)^2); 'half_height' is the width at half height (N = 5.54(t/W)^2). "
                "There is no default, because the two give different numbers on a real peak."
            )
        ),
    ],
    column_length_mm: Annotated[
        float | None,
        Field(
            default=None, gt=0, description="Column length in mm, if plates per metre are wanted."
        ),
    ] = None,
) -> PlateCountResult:
    """Column efficiency from one peak, by the USP <621> form matching the width given.

    USP defines the plate count two ways. They agree exactly for a Gaussian peak and diverge as it
    tails, so **a plate count is only comparable with a limit measured the same way**. This tool
    will not guess: `convention` is required, and the answer repeats it.

    A plate count is a property of the column *and* the peak used to measure it, not of the column
    alone — an early, poorly retained peak gives a lower count on the same column. It is evidence
    about efficiency at that retention, and it is not evidence that the column is fit for the
    separation, which is what resolution answers.

    Args:
        retention_time_min: The peak's retention time, in minutes.
        width_min: The peak width, in minutes, by `convention`.
        convention: 'tangent' or 'half_height'.
        column_length_mm: Column length in mm, if plates per metre are wanted.

    Returns:
        The plate count, its convention, and plates per metre when a length was given.

    Raises:
        ValueError: If a time, a width or the column length is not positive.
    """
    result = peaks.plate_count(retention_time_min, width_min, convention, column_length_mm)
    return PlateCountResult(
        plates=result.plates,
        convention=result.convention,
        plates_per_metre=result.plates_per_metre,
        basis=_PLATE_BASIS[convention],
    )


@server.tool()
def peak_symmetry(
    width_at_5_percent_min: Annotated[
        float, Field(gt=0, description="Full peak width at 5% of peak height, in minutes.")
    ],
    leading_half_width_min: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "`f` — the distance in minutes from the peak's leading edge at 5% height to the "
                "peak maximum. The front half of that width, not the whole of it."
            ),
        ),
    ],
) -> SymmetryResult:
    """The USP tailing (symmetry) factor, T = W_0.05 / 2f, measured at 5% of peak height.

    T = 1 is a symmetric peak, T > 1 tails, T < 1 fronts. The answer reports which, rather than
    folding it into the distance from 1, because the two have different causes: tailing usually
    means secondary interactions or an ionisable analyte near its pKa, while fronting usually
    means column overload or an injection solvent stronger than the mobile phase. A caller
    comparing |T - 1| to a threshold would treat them as one fault.

    Measured at 5% height, not 10% — the 10% figure is the *asymmetry factor* As, a different
    number from a different convention, and the two are routinely confused. This tool computes the
    USP one.

    Args:
        width_at_5_percent_min: The full width at 5% of peak height, in minutes.
        leading_half_width_min: `f`, leading edge to peak maximum at that height, in minutes.

    Returns:
        The tailing factor and the peak's skew direction.

    Raises:
        ValueError: If either measurement is not positive, or if `f` exceeds the whole width it is
            supposed to be part of — which means one of the two is not what it is labelled.
    """
    result = peaks.symmetry_factor(width_at_5_percent_min, leading_half_width_min)
    return SymmetryResult(
        tailing_factor=result.tailing_factor,
        shape=result.shape,
        basis="USP <621> T = W_0.05 / 2f, at 5% of peak height (not the 10% asymmetry factor As)",
    )


@server.tool()
def peak_resolution(
    first_retention_min: Annotated[
        float, Field(gt=0, description="Retention time of the earlier peak, in minutes.")
    ],
    second_retention_min: Annotated[
        float, Field(gt=0, description="Retention time of the later peak, in minutes.")
    ],
    first_width_min: Annotated[
        float, Field(gt=0, description="The earlier peak's width in minutes, by `convention`.")
    ],
    second_width_min: Annotated[
        float, Field(gt=0, description="The later peak's width in minutes, by `convention`.")
    ],
    convention: Annotated[
        Literal["tangent", "half_height"],
        Field(
            description=(
                "Which widths were measured. 'tangent' gives Rs = 2dt/(W1+W2), a definition; "
                "'half_height' gives Rs = 1.18dt/(W1+W2), which substitutes the Gaussian relation "
                "between the two widths and so approximates on a tailing peak."
            )
        ),
    ],
) -> ResolutionResult:
    """Resolution between two adjacent peaks, by the USP <621> form matching the widths given.

    The half-height form is the one a data system reports without being asked, and it is the
    optimistic one on exactly the peaks where resolution is in doubt: it assumes Gaussian peaks,
    and a tailing peak overlaps its neighbour more than its half-height width implies. The answer
    flags that assumption rather than leaving the caller to know it.

    Resolution is the criterion that actually answers "does this method separate these two
    things". A plate count can be excellent while a critical pair co-elutes.

    Args:
        first_retention_min: Retention time of the earlier peak, in minutes.
        second_retention_min: Retention time of the later peak, in minutes.
        first_width_min: The earlier peak's width, in minutes, by `convention`.
        second_width_min: The later peak's width, in minutes, by `convention`.
        convention: 'tangent' or 'half_height'.

    Returns:
        The resolution, its convention, and whether it rests on the Gaussian assumption.

    Raises:
        ValueError: If a width or time is not positive, or if the second peak does not elute after
            the first — resolution is defined between an earlier and a later peak.
    """
    result = peaks.resolution(
        first_retention_min, second_retention_min, first_width_min, second_width_min, convention
    )
    return ResolutionResult(
        resolution=result.resolution,
        convention=result.convention,
        assumes_gaussian=result.assumes_gaussian,
        basis=_RESOLUTION_BASIS[convention],
    )


@server.tool()
def retention_factor(
    retention_time_min: Annotated[
        float, Field(gt=0, description="The peak's retention time, in minutes.")
    ],
    void_time_min: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "`t_0`, the column void (hold-up) time in minutes — when an unretained marker "
                "elutes. Not the system dwell volume and not the gradient delay."
            ),
        ),
    ],
    second_retention_time_min: Annotated[
        float | None,
        Field(
            default=None,
            gt=0,
            description=(
                "A second, later peak's retention time in minutes, if the separation factor "
                "alpha = k2/k1 is wanted too."
            ),
        ),
    ] = None,
) -> RetentionResult:
    """k = (t_R - t_0)/t_0 for a peak, and alpha = k2/k1 if a second peak is given.

    The retention factor says how long a solute is held relative to the mobile phase. A peak at
    k <= 0 elutes at or before the void time: it is not retained, it is not being separated from
    anything else unretained, and a suitability criterion written for it is not measuring
    chromatography. The answer says so explicitly rather than returning a small or negative
    number.

    The separation factor is refused rather than computed when either peak is unretained, because
    a ratio taken anyway would be negative or undefined while looking like a selectivity.

    Args:
        retention_time_min: The peak's retention time, in minutes.
        void_time_min: The column void time `t_0`, in minutes.
        second_retention_time_min: A later peak's retention time, if alpha is wanted.

    Returns:
        The retention factor, whether the peak is unretained, and the second peak's k and alpha
        when a second peak was given.

    Raises:
        ValueError: If a time is not positive, or if alpha was asked for and either peak is
            unretained.
    """
    first = peaks.retention_factor(retention_time_min, void_time_min)
    second: peaks.RetentionFactor | None = None
    alpha: float | None = None
    if second_retention_time_min is not None:
        second = peaks.retention_factor(second_retention_time_min, void_time_min)
        alpha = peaks.separation_factor(first, second)
    return RetentionResult(
        retention_factor=first.retention_factor,
        unretained=first.unretained,
        second_retention_factor=second.retention_factor if second else None,
        separation_factor=alpha,
        basis="USP <621> k = (t_R - t_0)/t_0; alpha = k2/k1",
    )


@server.tool()
def permitted_method_adjustment(
    parameter: Annotated[
        Literal[
            "mobile_phase_ph",
            "buffer_concentration",
            "minor_mobile_phase_component",
            "column_length",
            "column_inner_diameter",
            "particle_size",
            "flow_rate",
            "injection_volume",
            "column_temperature",
            "detector_wavelength",
        ],
        Field(description="Which method parameter is being changed."),
    ],
    original: Annotated[
        float,
        Field(
            ge=0,
            description=(
                "The value the validated method specifies, in that parameter's own unit: pH "
                "units, mm, um, mL/min, uL, degrees C, nm, or % of the mobile phase."
            ),
        ),
    ],
    proposed: Annotated[
        float, Field(ge=0, description="The value being proposed, in the same unit.")
    ],
    is_gradient: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "True if the separation is a gradient. Gradient methods are refused rather than "
                "evaluated: <621> restricts their adjustment much further, and the isocratic "
                "allowance applied to a gradient is permissive in exactly the case that matters."
            ),
        ),
    ] = False,
) -> AdjustmentResult:
    """Is a proposed change within what USP <621> permits without revalidating the method?

    A compendial method may be adjusted within stated limits to meet system suitability; beyond
    them the change is a revalidation. This is the table a chemist looks up rather than remembers.

    **Two limits on what this answer means.** It reports what the *general chapter* permits — the
    section opens "unless otherwise specified in the individual monograph", and this tool cannot
    read the monograph, so a monograph limit overrides anything here. And it is for **isocratic**
    separations only; a gradient is refused by name rather than evaluated against the wrong table.

    The allowance is not always the one that looks obvious. A minor mobile-phase component may
    change by 30% *relative* to its own proportion, capped at 10 percentage points absolute — so
    5% organic may go to 6.5%, not to 15%. The answer names which of the two bounds actually
    applied.

    Args:
        parameter: Which method parameter is changing.
        original: The validated method's value, in that parameter's own unit.
        proposed: The proposed value, in the same unit.
        is_gradient: True for a gradient separation, which is refused.

    Returns:
        Whether the change is permitted, the limit it was measured against, and the reason.

    Raises:
        ValueError: If the separation is a gradient, if a value is negative, or if the original is
            zero for a parameter whose allowance is a percentage of it.
    """
    verdict = adjustments.check_adjustment(parameter, original, proposed, is_gradient=is_gradient)
    return AdjustmentResult(
        permitted=verdict.permitted,
        reason=verdict.reason,
        limit_value=verdict.limit_value,
        requested_relative_percent=verdict.requested_relative_percent,
        basis=(
            f"{adjustments.CHAPTER_BASIS}, isocratic. A monograph may specify otherwise and "
            "overrides this."
        ),
    )


class PeakInput(BaseModel):
    """One peak in a suitability table, as a chemist would read it off a chromatogram."""

    name: Annotated[str, Field(min_length=1, description="What the peak is called in the method.")]
    retention_time_min: Annotated[float, Field(gt=0, description="Retention time, in minutes.")]
    width_min: Annotated[
        float | None,
        Field(default=None, gt=0, description="Peak width in minutes, by the report's convention."),
    ] = None
    width_at_5_percent_min: Annotated[
        float | None,
        Field(default=None, gt=0, description="Full width at 5% height, in minutes, for tailing."),
    ] = None
    leading_half_width_min: Annotated[
        float | None,
        Field(default=None, gt=0, description="`f` at 5% height, in minutes, for tailing."),
    ] = None


@server.tool()
def system_suitability_report(
    peak_table: Annotated[
        list[PeakInput],
        Field(
            min_length=1,
            description=(
                "The peaks of one representative injection, in elution order. Resolution is "
                "computed between each peak and the one before it, so the order matters."
            ),
        ),
    ],
    convention: Annotated[
        Literal["tangent", "half_height"],
        Field(description="Which width convention `width_min` was measured under."),
    ],
    replicate_areas: Annotated[
        list[float],
        Field(
            default_factory=list, description="Replicate injection areas, if area RSD is wanted."
        ),
    ],
    replicate_retention_times_min: Annotated[
        list[float],
        Field(default_factory=list, description="Replicate retention times in minutes, if wanted."),
    ],
    area_rsd_limit_percent: Annotated[
        float | None,
        Field(default=None, gt=0, description="Area RSD limit in percent, if declared."),
    ] = None,
    retention_rsd_limit_percent: Annotated[
        float | None,
        Field(default=None, gt=0, description="Retention-time RSD limit in percent, if declared."),
    ] = None,
    minimum_resolution: Annotated[
        float | None,
        Field(default=None, gt=0, description="Minimum Rs between adjacent peaks, if declared."),
    ] = None,
    maximum_tailing_factor: Annotated[
        float | None, Field(default=None, gt=0, description="Maximum tailing factor, if declared.")
    ] = None,
    minimum_plates: Annotated[
        float | None, Field(default=None, gt=0, description="Minimum plate count, if declared.")
    ] = None,
) -> SuitabilityReport:
    """A whole injection sequence against a method's declared criteria, in one call.

    This composes the tools above over a pasted suitability table — it introduces no arithmetic of
    its own, so every number here is the one the corresponding single-peak tool would give.

    **`failures` empty does not mean the run is suitable.** It means every criterion that was
    *declared* passed. A criterion nobody passed in cannot be checked, and the common way a
    suitability review goes wrong is that the one limit nobody typed is the one that failed. Pass
    the limits the method actually declares.

    Args:
        peak_table: The peaks of one representative injection, in elution order.
        convention: Which width convention `width_min` was measured under.
        replicate_areas: Replicate injection areas, if area precision is wanted.
        replicate_retention_times_min: Replicate retention times, if retention precision is wanted.
        area_rsd_limit_percent: The method's area RSD limit, if declared.
        retention_rsd_limit_percent: The method's retention-time RSD limit, if declared.
        minimum_resolution: The method's minimum resolution between adjacent peaks, if declared.
        maximum_tailing_factor: The method's maximum tailing factor, if declared.
        minimum_plates: The method's minimum plate count, if declared.

    Returns:
        Per-peak metrics, the two precision results, and every declared criterion this run misses.

    Raises:
        ValueError: If more peaks or injections are sent than this tool prices, or if any single
            measurement is one the underlying tools refuse.
    """
    if len(peak_table) > MAX_PEAKS:
        raise ValueError(
            f"{len(peak_table)} peaks is more than this tool will take in one call ({MAX_PEAKS}). "
            "A suitability table names the peaks the criteria are written against."
        )
    _check_series(replicate_areas, "replicate areas")
    _check_series(replicate_retention_times_min, "replicate retention times")

    failures: list[str] = []
    reports: list[PeakReport] = []
    previous: PeakInput | None = None

    for peak in peak_table:
        plates: float | None = None
        if peak.width_min is not None:
            plates = peaks.plate_count(peak.retention_time_min, peak.width_min, convention).plates
            if minimum_plates is not None and plates < minimum_plates:
                failures.append(
                    f"{peak.name}: {plates:.0f} plates is below the declared minimum of "
                    f"{minimum_plates:.0f} ({convention} convention)"
                )

        tailing: float | None = None
        if peak.width_at_5_percent_min is not None and peak.leading_half_width_min is not None:
            symmetry = peaks.symmetry_factor(
                peak.width_at_5_percent_min, peak.leading_half_width_min
            )
            tailing = symmetry.tailing_factor
            if maximum_tailing_factor is not None and tailing > maximum_tailing_factor:
                failures.append(
                    f"{peak.name}: tailing factor {tailing:.2f} is above the declared maximum of "
                    f"{maximum_tailing_factor:.2f}"
                )

        separation: float | None = None
        if previous is not None and previous.width_min is not None and peak.width_min is not None:
            separation = peaks.resolution(
                previous.retention_time_min,
                peak.retention_time_min,
                previous.width_min,
                peak.width_min,
                convention,
            ).resolution
            if minimum_resolution is not None and separation < minimum_resolution:
                failures.append(
                    f"{previous.name}/{peak.name}: resolution {separation:.2f} is below the "
                    f"declared minimum of {minimum_resolution:.2f}"
                )

        reports.append(
            PeakReport(
                name=peak.name,
                retention_time_min=peak.retention_time_min,
                plates=plates,
                tailing_factor=tailing,
                resolution_from_previous=separation,
            )
        )
        previous = peak

    area = _precision_arm(replicate_areas, area_rsd_limit_percent, "area", failures)
    retention = _precision_arm(
        replicate_retention_times_min, retention_rsd_limit_percent, "retention time", failures
    )

    return SuitabilityReport(
        area_precision=area,
        retention_precision=retention,
        peaks=reports,
        failures=failures,
        basis=(
            f"USP <621> system suitability, {convention} width convention. Composed from the "
            "single-peak tools on this server; no criterion that was not declared was checked."
        ),
    )


def _precision_arm(
    values: list[float],
    limit: float | None,
    what: str,
    failures: list[str],
) -> ReplicatePrecisionResult | None:
    """One precision half of the report, appending its failures to the shared list.

    Returns `None` rather than raising when there are too few values: a report asked for peak
    metrics alone should not fail because no replicate series was pasted with it.
    """
    if len(values) < precision.MINIMUM_INJECTIONS:
        return None
    result = _precision_result(values, limit)
    if result.meets_limit is False:
        failures.append(
            f"{what} RSD {result.relative_standard_deviation_percent:.3f}% is above the declared "
            f"limit of {limit:.2f}%"
        )
    if result.injections_are_sufficient is False:
        failures.append(
            f"{what} precision used {result.injections} injections, where USP <621> requires "
            f"{result.injections_required} for a limit of {limit:.2f}%"
        )
    return result
