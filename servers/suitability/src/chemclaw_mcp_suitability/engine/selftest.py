"""The readiness check: run the arithmetic on cases whose answers are fixed outside this code.

The constants are a corpus that lives in source, so they are checked. USP's constants over-determine
each other: for a Gaussian peak the tangent width is 4 sigma and the half-height width 2.3548 sigma,
from which 5.54 and 1.18 were derived. So the two plate-count forms and the two resolution forms
must agree on a Gaussian within the published rounding, and a transposed digit (5.45, 1.81) breaks
that. The tolerances are derived from that rounding, not chosen. The adjustment table cannot be
derived, so it is digested and the digest published.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

from mcp_server_kit.datasets import Dataset

from chemclaw_mcp_suitability.engine import adjustments, peaks, precision

__all__ = ["CONSTANTS_VERSION", "SelfTestFailed", "verify"]

#: The version of the constants and allowance table this build serves; bump it by hand when a limit
#: or formula changes, so `/healthz` tells pods apart.
CONSTANTS_VERSION = "1.0.0"

#: The Gaussian width relations the USP constants derive from, written as expressions so the
#: derivation is visible.
_TANGENT_WIDTHS_PER_SIGMA = 4.0
_HALF_HEIGHT_WIDTHS_PER_SIGMA = 2.0 * math.sqrt(2.0 * math.log(2.0))

#: 16/4^2 = 1.0; 5.54/2.3548^2 = 0.99909 (5.545 rounded). 0.3% admits that rounding but not the
#: nearest transposition, 5.45 (1.7% off).
_PLATE_AGREEMENT = 0.003

#: 2/8 = 0.25; 1.18/(2 x 2.3548) = 0.25054 (1.1774 rounded), a 0.21% gap. 0.5% admits that and no
#: transposition.
_RESOLUTION_AGREEMENT = 0.005


class SelfTestFailed(RuntimeError):
    """A published or self-consistent value this server no longer reproduces.

    A `RuntimeError`, not a `ValueError`: the pod is wrong, not the caller, and `connector_app`
    treats it as permanent and takes the pod out of service.
    """


def _table_digest() -> str:
    """A digest over the transcribed <621> allowances, field by field.

    Built from the values, not the source file, so it changes when a limit moves and not when a
    comment does.
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
    # Both sides of the boundary: a tighter limit takes fewer injections.
    if precision.injections_required_for(2.0) != 5 or precision.injections_required_for(2.01) != 6:
        raise SelfTestFailed(
            "the <621> replicate rule no longer returns five injections at a 2.0% limit and six "
            "above it"
        )


def verify() -> list[Dataset]:
    """Recompute what this server is made of, and name the constants this pod serves.

    Returns:
        One `Dataset` describing the constants and the <621> allowances, with a digest over the
            table's values.

    Raises:
        SelfTestFailed: A check no longer reproduces; `connector_app` answers unready with the
            reason.
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
            # The constants are the modules, so the module is what is named.
            records_path=Path(adjustments.__file__),
        )
    ]
