"""The USP <621> allowances for adjusting a chromatographic method without revalidating it.

A compendial method may be adjusted within stated limits to meet system suitability; beyond them
the change is a revalidation. The limits are a short table with numbers in it, they are the thing
a chemist looks up rather than remembers, and getting one wrong is expensive in both directions —
too cautious revalidates a method that did not need it, too bold puts an unvalidated method into a
release test.

**Three things about this table constrain what the tool built on it may claim.**

*It is for isocratic separations.* USP restricts adjustments to gradient methods much further,
because a gradient's behaviour depends on the instrument's dwell volume as well as on the method,
and a change that is harmless on one system shifts selectivity on another. This module therefore
refuses a gradient method by name instead of applying the isocratic allowances to it, which is the
fleet's "refuse rather than approximate" rule at its most load-bearing: the approximation here
would be permissive.

*A monograph overrides it.* "Unless otherwise specified in the individual monograph" opens the
section, so an allowance computed here is what the general chapter permits, never a statement that
a particular monograph permits it. Every answer says so, because the tool cannot read the
monograph and the model reading the answer will not know that unless it is told.

*It is a table transcribed from a document, so it is exactly the kind of data that rots quietly.*
It is versioned and checksummed for that reason (see `engine/selftest.py`) — not because the
arithmetic is hard, but because a transposed digit in a limit nobody re-reads is invisible and
consequential, which is the same argument `servers/props` makes for validating its corpus against
itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = [
    "ADJUSTMENTS",
    "AdjustmentError",
    "AdjustmentVerdict",
    "Allowance",
    "Parameter",
    "check_adjustment",
]

#: The revision of USP General Chapter <621> this table was transcribed from. It is part of every
#: answer: an allowance is only meaningful beside the edition that grants it.
CHAPTER_BASIS = "USP General Chapter <621> Chromatography, Adjustments to Chromatographic Systems"

Parameter = Literal[
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
]


class AdjustmentError(ValueError):
    """An adjustment that cannot be evaluated against this table as written."""


@dataclass(frozen=True)
class Allowance:
    """What <621> permits for one parameter.

    The three bounds are deliberately separate rather than folded into one number, because they
    are different *kinds* of limit and a single "tolerance" would lose which one bit.

    Attributes:
        relative_percent: The permitted change as a percentage of the original value, or `None`
            where the limit is not expressed that way.
        absolute: The permitted change in the parameter's own unit, or `None`. Where both this
            and `relative_percent` are set, `absolute_is_a_cap` says how they combine.
        absolute_is_a_cap: True when the absolute bound *caps* the relative one (the relative
            allowance applies, but never beyond the absolute figure); False when the absolute
            bound is the only limit there is.
        direction: Which way the change may go. "either" for a symmetric allowance, "decrease"
            where only a reduction is permitted, "none" where no adjustment is allowed at all.
        unit: The parameter's unit, for the message. Empty where the parameter is dimensionless.
        note: What a chemist needs to know that the numbers do not say.
    """

    relative_percent: float | None
    absolute: float | None
    absolute_is_a_cap: bool
    direction: Literal["either", "decrease", "none"]
    unit: str
    note: str


#: The table. Every entry is one row of the chapter, and the notes are why a row is not just a
#: number. Keep this dict and `selftest._TABLE_DIGEST` in step: the digest is what makes an
#: unreviewed edit to a limit visible from a readiness probe.
ADJUSTMENTS: dict[Parameter, Allowance] = {
    "mobile_phase_ph": Allowance(
        relative_percent=None,
        absolute=0.2,
        absolute_is_a_cap=False,
        direction="either",
        unit="pH units",
        note=(
            "pH is adjusted on the aqueous buffer before mixing with the organic, not on the "
            "mixed mobile phase, and the two are not the same measurement."
        ),
    ),
    "buffer_concentration": Allowance(
        relative_percent=10.0,
        absolute=None,
        absolute_is_a_cap=False,
        direction="either",
        unit="",
        note="The salt concentration of the buffer, as a relative change.",
    ),
    "minor_mobile_phase_component": Allowance(
        relative_percent=30.0,
        absolute=10.0,
        absolute_is_a_cap=True,
        direction="either",
        unit="% of the mobile phase",
        note=(
            "Applies to a component specified at 50% or less. The 30% is *relative* to that "
            "component's own proportion, and the absolute change may not exceed 10 percentage "
            "points however small that relative figure looks: 5% organic may go to 6.5%, not to "
            "15%."
        ),
    ),
    "column_length": Allowance(
        relative_percent=70.0,
        absolute=None,
        absolute_is_a_cap=False,
        direction="either",
        unit="mm",
        note=(
            "Length and particle size move together in practice; where both change, the "
            "length-to-particle-size ratio is the quantity the chapter constrains."
        ),
    ),
    "column_inner_diameter": Allowance(
        relative_percent=25.0,
        absolute=None,
        absolute_is_a_cap=False,
        direction="either",
        unit="mm",
        note=(
            "A diameter change rescales the flow rate for the same linear velocity; the flow "
            "rate that follows from it is a separate adjustment with its own allowance."
        ),
    ),
    "particle_size": Allowance(
        relative_percent=50.0,
        absolute=None,
        absolute_is_a_cap=False,
        direction="decrease",
        unit="um",
        note=(
            "A reduction of up to 50% is permitted; an increase is not, at any size. A smaller "
            "particle raises efficiency and back pressure, and the chapter allows the first."
        ),
    ),
    "flow_rate": Allowance(
        relative_percent=50.0,
        absolute=None,
        absolute_is_a_cap=False,
        direction="either",
        unit="mL/min",
        note="",
    ),
    "injection_volume": Allowance(
        relative_percent=None,
        absolute=None,
        absolute_is_a_cap=False,
        direction="decrease",
        unit="uL",
        note=(
            "May be reduced as far as precision and detection still allow, which is a judgement "
            "about this method's own limits rather than a number this table can supply. An "
            "increase is not permitted."
        ),
    ),
    "column_temperature": Allowance(
        relative_percent=None,
        absolute=10.0,
        absolute_is_a_cap=False,
        direction="either",
        unit="degrees C",
        note="Requires a thermostatted column; an ambient column is not a controlled temperature.",
    ),
    "detector_wavelength": Allowance(
        relative_percent=None,
        absolute=None,
        absolute_is_a_cap=False,
        direction="none",
        unit="nm",
        note=(
            "No adjustment is permitted. The wavelength is specified in the method and a change "
            "alters the response factor of every analyte and impurity differently."
        ),
    ),
}


@dataclass(frozen=True)
class AdjustmentVerdict:
    """Whether one proposed change is inside what <621> permits."""

    parameter: Parameter
    original: float
    proposed: float
    permitted: bool
    #: The reason, written for a chemist. Populated whether or not the change is permitted,
    #: because "why is this allowed" is asked as often as "why is this not".
    reason: str
    #: The widest value the allowance reaches in the direction of travel, or `None` where the
    #: allowance is not a number (no change permitted, or a reduction bounded by judgement).
    limit_value: float | None
    #: The change actually asked for, as a percentage of the original. `None` when the original
    #: is zero, where a relative change is undefined rather than infinite.
    requested_relative_percent: float | None


def check_adjustment(
    parameter: Parameter,
    original: float,
    proposed: float,
    *,
    is_gradient: bool = False,
) -> AdjustmentVerdict:
    """Is a proposed change within the <621> allowance for its parameter?

    Args:
        parameter: Which method parameter is changing.
        original: The value the validated method specifies, in the parameter's own unit.
        proposed: The value being proposed, in the same unit.
        is_gradient: True if the separation is a gradient. Gradient methods are refused rather
            than evaluated, because the isocratic allowances do not carry to them.

    Returns:
        The verdict, the limit it was measured against, and the reason in words.

    Raises:
        AdjustmentError: If the separation is a gradient, or if a value is negative, or if the
            original is zero for a parameter whose allowance is relative.
    """
    if is_gradient:
        raise AdjustmentError(
            "these allowances are for isocratic separations. USP <621> restricts adjustments to "
            "gradient methods considerably further, because a gradient's selectivity depends on "
            "the instrument's dwell volume as well as on the method, so an isocratic allowance "
            "applied to a gradient would be permissive in exactly the case that matters. Evaluate "
            "a gradient change against the monograph and the chapter's gradient provisions."
        )
    allowance = ADJUSTMENTS[parameter]
    if original < 0.0 or proposed < 0.0:
        raise AdjustmentError(
            f"a {parameter.replace('_', ' ')} cannot be negative; got original={original}, "
            f"proposed={proposed}."
        )

    change = proposed - original
    relative: float | None = None
    if original != 0.0:
        relative = 100.0 * change / original

    if allowance.direction == "none":
        return AdjustmentVerdict(
            parameter=parameter,
            original=original,
            proposed=proposed,
            permitted=change == 0.0,
            reason=(
                "no adjustment is permitted for this parameter. " + allowance.note
                if change != 0.0
                else "unchanged, so there is nothing to permit."
            ),
            limit_value=original,
            requested_relative_percent=relative,
        )

    if allowance.direction == "decrease" and change > 0.0:
        return AdjustmentVerdict(
            parameter=parameter,
            original=original,
            proposed=proposed,
            permitted=False,
            reason=(
                f"only a reduction is permitted for this parameter, and {proposed} is above the "
                f"specified {original}. " + allowance.note
            ),
            limit_value=original,
            requested_relative_percent=relative,
        )

    if allowance.relative_percent is None and allowance.absolute is None:
        # A reduction bounded by judgement rather than by a number — injection volume.
        return AdjustmentVerdict(
            parameter=parameter,
            original=original,
            proposed=proposed,
            permitted=True,
            reason=(
                "the chapter sets no numeric floor here, so this is permitted as far as this "
                "table can say. " + allowance.note
            ),
            limit_value=None,
            requested_relative_percent=relative,
        )

    bound = _bound_for(allowance, original, parameter)
    permitted = abs(change) <= bound + _tolerance(bound)
    limit_value = original + bound if change >= 0.0 else original - bound
    return AdjustmentVerdict(
        parameter=parameter,
        original=original,
        proposed=proposed,
        permitted=permitted,
        reason=_reason(allowance, original, bound, change, permitted),
        limit_value=limit_value,
        requested_relative_percent=relative,
    )


def _bound_for(allowance: Allowance, original: float, parameter: Parameter) -> float:
    """The widest change permitted, in the parameter's own unit.

    Where a relative allowance is capped by an absolute one, the cap is applied here rather than
    at the comparison, so the number reported back as the limit is the one that actually bound.
    """
    if allowance.relative_percent is None:
        if allowance.absolute is None:  # pragma: no cover - guarded by the caller
            raise AdjustmentError(f"{parameter} has no numeric allowance to compare against.")
        return allowance.absolute
    if original == 0.0:
        raise AdjustmentError(
            f"the allowance for {parameter.replace('_', ' ')} is a percentage of the specified "
            "value, and the specified value given is zero, so there is nothing to take a "
            "percentage of."
        )
    relative_bound = abs(original) * allowance.relative_percent / 100.0
    if allowance.absolute is not None and allowance.absolute_is_a_cap:
        return min(relative_bound, allowance.absolute)
    return relative_bound


def _tolerance(bound: float) -> float:
    """A float-comparison slack, so a change exactly at the limit is not failed by rounding.

    A proposed value stated to the precision a chemist writes it in — 1.5 mL/min from 1.0 — should
    read as exactly at the 50% limit, and binary floating point does not always agree. The slack
    is relative to the bound and far below any real method's precision.
    """
    return abs(bound) * 1e-9


def _reason(
    allowance: Allowance, original: float, bound: float, change: float, permitted: bool
) -> str:
    """The verdict in words, naming which of the two bounds actually applied."""
    capped = (
        allowance.absolute is not None
        and allowance.absolute_is_a_cap
        and allowance.relative_percent is not None
        and bound == allowance.absolute
    )
    if allowance.relative_percent is not None and not capped:
        basis = f"{allowance.relative_percent:g}% of the specified {original:g}"
    elif capped:
        basis = (
            f"the absolute cap of {allowance.absolute:g} {allowance.unit}, which is tighter here "
            f"than the {allowance.relative_percent:g}% relative allowance"
        )
    else:
        basis = f"{allowance.absolute:g} {allowance.unit}".strip()
    verdict = "within" if permitted else "beyond"
    tail = f" {allowance.note}" if allowance.note else ""
    return (
        f"a change of {change:+g} {allowance.unit}".rstrip()
        + f" is {verdict} the permitted {bound:g} {allowance.unit}".rstrip()
        + f", which is {basis}.{tail}"
    )
