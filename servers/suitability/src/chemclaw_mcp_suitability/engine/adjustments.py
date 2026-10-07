"""The USP <621> allowances for adjusting a chromatographic method without revalidating it.

Beyond these limits a change is a revalidation, and an error is costly either way. Three constraints
bound what the tool may claim:

- **Isocratic only.** A gradient's behaviour depends on the instrument's dwell volume, so gradient
  methods are refused rather than given the (permissive) isocratic allowances.
- **A monograph overrides it.** An allowance here is what the general chapter permits, never what a
  particular monograph permits; every answer says so.
- **It is transcribed data**, so it is versioned and checksummed (`engine/selftest.py`) to make a
  transposed digit visible.
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

#: The USP <621> revision this table was transcribed from; part of every answer.
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

    The bounds are separate because they are different kinds of limit.

    Attributes:
        relative_percent: The permitted change as a percentage of the original value, or `None`.
        absolute: The permitted change in the parameter's own unit, or `None`.
        absolute_is_a_cap: True when the absolute bound caps the relative one; False when it is the
            only limit.
        direction: "either", "decrease" (only a reduction permitted) or "none" (no adjustment).
        unit: The parameter's unit, for the message; empty where dimensionless.
        note: What a chemist needs to know that the numbers do not say.
    """

    relative_percent: float | None
    absolute: float | None
    absolute_is_a_cap: bool
    direction: Literal["either", "decrease", "none"]
    unit: str
    note: str


#: The table, one entry per row of the chapter. Keep it and `selftest._TABLE_DIGEST` in step, so an
#: unreviewed edit fails readiness.
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
    #: The reason, for a chemist; populated whether or not the change is permitted.
    reason: str
    #: The widest value the allowance reaches in the direction of travel, or `None` where the
    #: allowance is not a number (no change permitted, or a reduction bounded by judgement).
    limit_value: float | None
    #: The requested change as a percentage of the original; `None` when the original is zero.
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
        is_gradient: True if the separation is a gradient; gradient methods are refused.

    Returns:
        The verdict, the limit it was measured against, and the reason in words.

    Raises:
        AdjustmentError: The separation is a gradient, a value is negative, or the original is zero
            for a relative allowance.
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

    An absolute cap is applied here so the reported limit is the one that actually bound.
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

    Relative to the bound and far below any real method's precision.
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
