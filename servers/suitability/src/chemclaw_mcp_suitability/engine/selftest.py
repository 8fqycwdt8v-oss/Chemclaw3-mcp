"""The readiness check: run the arithmetic on cases whose answers are fixed outside this code.

This server loads no corpus, and `servers/thermalsafety` already settled what that does and does
not excuse (`D-2026-09-15-a-server-with-nothing-to-load-still-has-something-to-verify`): a probe is
owed anyway, because the constants *are* a vendored corpus that happens to live in Python source,
and a transposed digit in one is invisible to a checksum nobody computes.

**What makes the check here unusually strong is that USP's own constants over-determine each
other.** The chapter gives two plate-count forms and two resolution forms:

    N = 16 (t_R/W)^2        N = 5.54 (t_R/W_0.5)^2
    Rs = 2 dt/(W1 + W2)     Rs = 1.18 dt/(W_0.5,1 + W_0.5,2)

For a Gaussian peak the tangent width is 4 sigma and the half-height width is 2 sqrt(2 ln 2) sigma
= 2.3548 sigma, and those relations are what the constants 5.54 and 1.18 were *derived from*. So
the two forms must agree on a Gaussian to within the rounding in the published constants — and
they do, to 0.09% and 0.21% respectively. That is a real independent check rather than a
restatement: a transposed 5.54 -> 5.45, or 1.18 -> 1.81, breaks the agreement immediately, while a
probe that merely called each function and looked for a number would pass both.

The agreement tolerances below are therefore **derived, not chosen**: 5.54 is 5.545 rounded, and
1.18 is 1.1774 rounded, so the disagreement each rounding forces is arithmetic, and the tolerance
is that figure with a margin rather than a number picked until the test went green.

The adjustment table cannot be checked that way — a regulatory allowance is not derivable from
anything — so it is digested instead, and the digest is what makes an unreviewed edit to a limit
visible from a scrape rather than from a code review that already happened.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

from mcp_server_kit.datasets import Dataset

from chemclaw_mcp_suitability.engine import adjustments, peaks, precision

__all__ = ["CONSTANTS_VERSION", "SelfTestFailed", "verify"]

#: The version of the first-party constants and the transcribed allowance table this build serves.
#: Bumped by hand in the commit that changes a limit or a formula, so an operator reading
#: `/healthz` can tell two pods apart without a shell on either.
CONSTANTS_VERSION = "1.0.0"

#: The Gaussian width relations the USP constants were derived from. Written here as expressions
#: rather than as decimals so that the derivation is visible and cannot drift from its own source.
_TANGENT_WIDTHS_PER_SIGMA = 4.0
_HALF_HEIGHT_WIDTHS_PER_SIGMA = 2.0 * math.sqrt(2.0 * math.log(2.0))

#: 16/4^2 = 1.0 exactly; 5.54/2.3548^2 = 0.99909. The published 5.54 is 5.545 rounded down, so the
#: two plate-count forms must disagree by that rounding and no more. 0.3% leaves room for the
#: rounding (0.09%) without admitting a transposed digit (the nearest, 5.45, is off by 1.7%).
_PLATE_AGREEMENT = 0.003

#: 2/8 = 0.25; 1.18/(2 x 2.3548) = 0.25054. The published 1.18 is 1.1774 rounded up, forcing a
#: 0.21% disagreement. 0.5% admits that and nothing near a transposition.
_RESOLUTION_AGREEMENT = 0.005


class SelfTestFailed(RuntimeError):
    """A published or self-consistent value this server no longer reproduces.

    `RuntimeError` rather than `ValueError`: this is never a caller's input, it is this pod being
    wrong, and `connector_app` classifies it as a permanent cause so the pod is taken out of its
    Service rather than left serving arithmetic that has moved.
    """


def _table_digest() -> str:
    """A digest over the transcribed <621> allowances, field by field.

    Built from the table's *values* rather than from the source file, so a comment or a docstring
    edit does not change it while a changed limit always does. That is the distinction that makes
    the digest worth publishing: it answers "has an allowance moved", not "has the file been
    touched".
    """
    digest = hashlib.sha256()
    for name in sorted(adjustments.ADJUSTMENTS):
        allowance = adjustments.ADJUSTMENTS[name]
        digest.update(
            "|".join(
                (
                    name,
                    repr(allowance.relative_percent),
                    repr(allowance.absolute),
                    repr(allowance.absolute_is_a_cap),
                    allowance.direction,
                    allowance.unit,
                )
            ).encode("utf-8")
        )
    return digest.hexdigest()


def _check_plate_constants() -> None:
    """The two plate-count forms must agree on a Gaussian peak, because 5.54 came from 16."""
    sigma = 0.05
    retention = 6.0
    tangent = peaks.plate_count(retention, _TANGENT_WIDTHS_PER_SIGMA * sigma, "tangent")
    half = peaks.plate_count(retention, _HALF_HEIGHT_WIDTHS_PER_SIGMA * sigma, "half_height")
    disagreement = abs(tangent.plates - half.plates) / tangent.plates
    if disagreement > _PLATE_AGREEMENT:
        raise SelfTestFailed(
            f"the two plate-count conventions disagree by {disagreement:.2%} on a Gaussian peak, "
            f"where the rounding in USP's published constants forces at most {_PLATE_AGREEMENT:.2%}"
            f" (tangent {tangent.plates:.1f}, half-height {half.plates:.1f}). One of the two "
            "constants is wrong."
        )


def _check_resolution_constants() -> None:
    """The two resolution forms must likewise agree, because 1.18 came from 2."""
    sigma = 0.04
    first, second = 5.0, 5.4
    tangent = peaks.resolution(
        first,
        second,
        _TANGENT_WIDTHS_PER_SIGMA * sigma,
        _TANGENT_WIDTHS_PER_SIGMA * sigma,
        "tangent",
    )
    half = peaks.resolution(
        first,
        second,
        _HALF_HEIGHT_WIDTHS_PER_SIGMA * sigma,
        _HALF_HEIGHT_WIDTHS_PER_SIGMA * sigma,
        "half_height",
    )
    disagreement = abs(tangent.resolution - half.resolution) / tangent.resolution
    if disagreement > _RESOLUTION_AGREEMENT:
        raise SelfTestFailed(
            f"the two resolution conventions disagree by {disagreement:.2%} on a pair of Gaussian "
            f"peaks, where the rounding in USP's constants forces at most "
            f"{_RESOLUTION_AGREEMENT:.2%} (tangent {tangent.resolution:.4f}, half-height "
            f"{half.resolution:.4f}). One of the two constants is wrong."
        )


def _check_symmetry() -> None:
    """A peak whose leading half is exactly half its width has T = 1, by definition."""
    symmetric = peaks.symmetry_factor(width_at_5_percent_min=0.40, leading_half_width_min=0.20)
    if symmetric.tailing_factor != 1.0 or symmetric.shape != "symmetric":
        raise SelfTestFailed(
            f"a peak with f exactly half its 5% width computed T = "
            f"{symmetric.tailing_factor} ({symmetric.shape}), where the definition gives exactly 1"
        )


def _check_precision() -> None:
    """RSD against a hand-computable series, and the <621> injection rule at its boundary."""
    # [1, 2, 3, 4, 5]: mean 3, s = sqrt(2.5) over n-1, so RSD = 100 sqrt(2.5)/3 = 52.7046%.
    measured = precision.relative_standard_deviation([1.0, 2.0, 3.0, 4.0, 5.0])
    expected = 100.0 * math.sqrt(2.5) / 3.0
    if abs(measured.relative_standard_deviation_percent - expected) > 1e-9:
        raise SelfTestFailed(
            f"the RSD of [1,2,3,4,5] computed "
            f"{measured.relative_standard_deviation_percent:.6f}% where the sample standard "
            f"deviation gives {expected:.6f}%. A population denominator would give "
            f"{100.0 * math.sqrt(2.0) / 3.0:.6f}%."
        )
    # The rule runs the counter-intuitive way round, so both sides of the boundary are checked:
    # a tighter limit takes fewer injections, not more.
    if precision.injections_required_for(2.0) != 5 or precision.injections_required_for(2.01) != 6:
        raise SelfTestFailed(
            "the <621> replicate rule no longer returns five injections at a 2.0% limit and six "
            "above it"
        )


def verify() -> list[Dataset]:
    """Recompute what this server is made of, and name the constants this pod serves.

    Returns:
        One `Dataset` describing the first-party constants and the transcribed <621> allowances,
        with a digest over the allowance table's values.

    Raises:
        SelfTestFailed: If any check no longer reproduces. `connector_app` turns this into an
            unready `/healthz` naming the reason.
    """
    _check_plate_constants()
    _check_resolution_constants()
    _check_symmetry()
    _check_precision()

    return [
        Dataset(
            name="suitability-constants",
            version=CONSTANTS_VERSION,
            licence="first-party",
            retrieved_from=(
                "USP General Chapter <621> Chromatography (system suitability parameters and the "
                "allowances for adjusting a chromatographic system)"
            ),
            description=(
                "The USP <621> formulas this server computes with and the allowance table it "
                "checks a proposed method change against. The formulas are verified on every "
                "probe by their own mutual consistency on a Gaussian peak; the allowances are "
                "digested, because a regulatory limit is not derivable from anything."
            ),
            sha256=_table_digest(),
            # The constants *are* the modules, so a module is what is named. Pointing this at a
            # records file that does not exist would be a provenance field that quietly says
            # nothing.
            records_path=Path(adjustments.__file__),
        )
    ]
