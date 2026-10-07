"""Fenske-Underwood-Gilliland: the shortcut column, for a binary key pair at constant volatility.

- **Fenske** gives `N_min`, stages at total reflux; exact for constant alpha.
- **Underwood** gives `R_min`, below which the split is unreachable at any stage count; exact, via
  the root `θ` of `Σ alpha_i·z_i/(alpha_i - θ) = 1 - q`, found by bisection on `(1, alpha)` where a
  sign change is guaranteed.
- **Gilliland** (Molokanov's closed form) gives `N` at a chosen `R`; an empirical correlation with
  ±10-20% scatter, worst near `R_min`.

A screening arithmetic, not a stage-by-stage simulation: no VLE data, and one alpha for the whole
column (false for azeotropic or strongly non-ideal systems). It returns theoretical stages including
the reboiler and nothing about efficiency, diameter, pressure drop or hold-up. Alpha is an input
from measured or literature data; a boiling-point ratio is not alpha.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from chemclaw_mcp_unitops.engine.validation import UnitOpsInputError, finite, fraction, positive

__all__ = [
    "UNDERWOOD_BISECTION_STEPS",
    "ShortcutColumn",
    "fenske_minimum_stages",
    "gilliland_stages",
    "shortcut_column",
    "underwood_minimum_reflux",
]

#: A ceiling on bisection steps, not a step count: the loop stops once the interval is one float
#: wide, long before this.
UNDERWOOD_BISECTION_STEPS = 200


def _volatility(alpha: float) -> float:
    """The relative volatility, refused at or below 1 by name rather than by arithmetic."""
    if alpha <= 1.0:
        raise UnitOpsInputError(
            f"the relative volatility is {alpha}, which is at or below 1. At alpha = 1 the two "
            "components have the same volatility and no number of stages separates them — an "
            "azeotrope at the composition of interest reads exactly like this. Below 1 the keys "
            "are the wrong way round: swap them."
        )
    return alpha


def fenske_minimum_stages(
    *, relative_volatility: float, light_key_in_distillate: float, light_key_in_bottoms: float
) -> float:
    """`N_min`: theoretical stages at total reflux, including the reboiler.

        N_min = ln[ (x_D/(1-x_D)) · ((1-x_B)/x_B) ] / ln alpha

    Args:
        relative_volatility: `alpha` of the light key over the heavy key, constant over the column;
        above 1. light_key_in_distillate: Mole fraction of the light key in the distillate, 0 to 1.
        light_key_in_bottoms: Mole fraction of the light key in the bottoms, 0 to 1.

    Returns:
        The minimum number of theoretical stages, including the reboiler as one stage.

    Raises:
        UnitOpsInputError: `alpha` at or below 1, a fraction outside `(0, 1)`, or bottoms not leaner
        in the light key than the distillate.
    """
    alpha = _volatility(relative_volatility)
    top = fraction(light_key_in_distillate, "the light key in the distillate")
    bottom = fraction(light_key_in_bottoms, "the light key in the bottoms")
    if bottom >= top:
        raise UnitOpsInputError(
            f"the bottoms are given as {bottom} light key and the distillate as {top}, so the "
            "light key is not enriched overhead. The light key is the component that goes up; if "
            "these are the right numbers, the keys are swapped."
        )
    return math.log((top / (1.0 - top)) * ((1.0 - bottom) / bottom)) / math.log(alpha)


def underwood_minimum_reflux(
    *,
    relative_volatility: float,
    light_key_in_feed: float,
    light_key_in_distillate: float,
    feed_quality: float = 1.0,
) -> tuple[float, float]:
    """`R_min` and Underwood's root `θ`, for a binary key pair at constant `alpha`.

    Solves for `θ` in `(1, alpha)`:

        alpha·z/(alpha - θ) + (1 - z)/(1 - θ) = 1 - q

    then evaluates:

        R_min + 1 = alpha·x_D/(alpha - θ) + (1 - x_D)/(1 - θ)

    The left side rises monotonically from -∞ to +∞ across the bracket, so bisection always finds
    the one root.

    Args:
        relative_volatility: `alpha`, dimensionless and above 1.
        light_key_in_feed: Mole fraction of the light key in the feed, 0 to 1.
        light_key_in_distillate: Mole fraction of the light key in the distillate, 0 to 1.
        feed_quality: `q`: 1 saturated liquid (default, the usual tank-fed case), 0 saturated
        vapour, >1 subcooled liquid, <0 superheated vapour.

    Returns:
        `(R_min, θ)`; `θ` shows the root lies in `(1, alpha)`.

    Raises:
        UnitOpsInputError: `alpha` at or below 1, a composition outside `(0, 1)`, a non-finite `q`,
        or a distillate not richer in the light key than the feed.
    """
    alpha = _volatility(relative_volatility)
    feed = fraction(light_key_in_feed, "the light key in the feed")
    top = fraction(light_key_in_distillate, "the light key in the distillate")
    # Checked here: a NaN `q` would make the refusal below blame the composition instead.
    q = finite(feed_quality, "the feed quality q")
    if top <= feed:
        raise UnitOpsInputError(
            f"the distillate is given as {top} light key against a feed of {feed}, so the column "
            "is not enriching. A distillate no richer than its feed needs no column."
        )

    def residual(theta: float) -> float:
        return alpha * feed / (alpha - theta) + (1.0 - feed) / (1.0 - theta) - (1.0 - q)

    low, high = 1.0, alpha
    for _ in range(UNDERWOOD_BISECTION_STEPS):
        middle = 0.5 * (low + high)
        # The endpoints are poles; stop once the midpoint is an endpoint rather than evaluate one.
        if middle <= low or middle >= high:
            break
        if residual(middle) < 0.0:
            low = middle
        else:
            high = middle
    theta = 0.5 * (low + high)
    if not 1.0 < theta < alpha:
        raise UnitOpsInputError(
            f"Underwood's root landed on the edge of its own interval (θ = {theta!r} against "
            f"alpha = {alpha}), so there is no finite minimum reflux to report. The root is driven "
            "onto an endpoint by a feed holding almost none of one key, or by a relative "
            "volatility almost exactly 1 — in both cases the column being described is not one "
            "anybody would build. Check the feed composition and the volatility."
        )
    minimum_reflux = (alpha * top / (alpha - theta) + (1.0 - top) / (1.0 - theta)) - 1.0
    return minimum_reflux, theta


def gilliland_stages(*, minimum_stages: float, minimum_reflux: float, reflux_ratio: float) -> float:
    """`N` at a chosen reflux ratio, by Molokanov's closed form of the Gilliland correlation.

        X = (R - R_min)/(R + 1)
        Y = 1 - exp[ ((1 + 54.4X)/(11 + 117.2X)) · ((X - 1)/√X) ]
        N = (Y + N_min)/(1 - Y)

    An empirical correlation: ±10-20% on `N` is ordinary, widest near `R_min`.

    Args:
        minimum_stages: `N_min` from Fenske.
        minimum_reflux: `R_min` from Underwood.
        reflux_ratio: The `R = L/D` the column will run at; must exceed `R_min` (designs sit at
        1.05-1.5 times it).

    Returns:
        The theoretical stages at that reflux, including the reboiler.

    Raises:
        UnitOpsInputError: The reflux ratio is not finite, is at or below the minimum (infinite or
        unreachable stage count), or is so close above it that the stage count overflows.
    """
    positive(minimum_stages, "the minimum stage count")
    positive(minimum_reflux, "the minimum reflux ratio")
    # Finite first, so an explicit `inf` is refused.
    if positive(reflux_ratio, "the reflux ratio") <= minimum_reflux:
        raise UnitOpsInputError(
            f"the reflux ratio of {reflux_ratio:.4g} is at or below the minimum of "
            f"{minimum_reflux:.4g}. At the minimum the column needs infinitely many stages, and "
            "below it this split cannot be reached at any stage count. Design reflux is normally "
            "1.05 to 1.5 times the minimum."
        )
    x = (reflux_ratio - minimum_reflux) / (reflux_ratio + 1.0)
    y = 1.0 - math.exp(((1.0 + 54.4 * x) / (11.0 + 117.2 * x)) * ((x - 1.0) / math.sqrt(x)))
    # Just above `R_min`, `exp` underflows and `Y` is exactly 1.0: the stage count is unbounded in
    # floating point, so refuse as at `R_min`.
    if not math.isfinite(y) or y >= 1.0:
        raise UnitOpsInputError(
            f"the reflux ratio of {reflux_ratio:.6g} is so close to the minimum of "
            f"{minimum_reflux:.6g} that the stage count is unbounded. Design reflux is normally "
            "1.05 to 1.5 times the minimum."
        )
    return (y + minimum_stages) / (1.0 - y)


@dataclass(frozen=True)
class ShortcutColumn:
    """One shortcut design: the two bounds and the stage count at the chosen reflux."""

    minimum_stages: float
    minimum_reflux_ratio: float
    #: Underwood's root, between 1 and `alpha` by construction.
    underwood_theta: float
    reflux_ratio: float
    #: `R/R_min`, the number a design is argued over (typically 1.05-1.5).
    reflux_over_minimum: float
    theoretical_stages: float
    #: `N - N_min`, the stages bought by running above the minimum reflux.
    stages_above_minimum: float


def shortcut_column(
    *,
    relative_volatility: float,
    light_key_in_feed: float,
    light_key_in_distillate: float,
    light_key_in_bottoms: float,
    reflux_ratio: float | None = None,
    reflux_over_minimum: float = 1.3,
    feed_quality: float = 1.0,
) -> ShortcutColumn:
    """Fenske, Underwood and Gilliland in one answer, because nobody wants one of the three.

    Args:
        relative_volatility: `alpha` of the light key over the heavy key, constant over the column.
        light_key_in_feed: Mole fraction of the light key in the feed.
        light_key_in_distillate: Mole fraction of the light key in the distillate.
        light_key_in_bottoms: Mole fraction of the light key in the bottoms.
        reflux_ratio: The `R = L/D` to evaluate at; when omitted, `reflux_over_minimum` times
        `R_min`. reflux_over_minimum: The multiple of `R_min` used when no reflux is given; 1.3 is a
        convention, and the returned `reflux_ratio` says what was used. feed_quality: `q`; 1 for a
        saturated liquid.

    Returns:
        The minimum stages, the minimum reflux, the reflux actually used and the stages it needs.

    Raises:
        UnitOpsInputError: As the three functions above, or compositions not ordered `x_B < z <
        x_D`.
    """
    # The overall balance `F·z = D·x_D + B·x_B` needs `x_B < z < x_D`; Fenske and Underwood each
    # check only part of that, so check it before either runs.
    if fraction(light_key_in_bottoms, "the light key in the bottoms") >= fraction(
        light_key_in_feed, "the light key in the feed"
    ):
        raise UnitOpsInputError(
            f"the bottoms are given as {light_key_in_bottoms} light key against a feed of "
            f"{light_key_in_feed}, so both products would be richer in the light key than the feed "
            "is. No split of a feed can do that: the overall balance F·z = D·x_D + B·x_B has no "
            "solution in positive flows. Check which stream each composition belongs to."
        )
    minimum_stages = fenske_minimum_stages(
        relative_volatility=relative_volatility,
        light_key_in_distillate=light_key_in_distillate,
        light_key_in_bottoms=light_key_in_bottoms,
    )
    minimum_reflux, theta = underwood_minimum_reflux(
        relative_volatility=relative_volatility,
        light_key_in_feed=light_key_in_feed,
        light_key_in_distillate=light_key_in_distillate,
        feed_quality=feed_quality,
    )
    if minimum_reflux <= 0.0:
        raise UnitOpsInputError(
            f"Underwood's minimum reflux computes as {minimum_reflux:.4g}, which is not positive. "
            "That happens when the feed quality q puts the feed far from a saturated liquid "
            "(largely vapour, or strongly subcooled) or the specified split is reachable by a "
            "flash rather than by a column; a shortcut column is not the arithmetic for it."
        )
    if reflux_ratio is None:
        positive(reflux_over_minimum, "the multiple of the minimum reflux")
        reflux_ratio = reflux_over_minimum * minimum_reflux
    stages = gilliland_stages(
        minimum_stages=minimum_stages,
        minimum_reflux=minimum_reflux,
        reflux_ratio=reflux_ratio,
    )
    return ShortcutColumn(
        minimum_stages=minimum_stages,
        minimum_reflux_ratio=minimum_reflux,
        underwood_theta=theta,
        reflux_ratio=reflux_ratio,
        reflux_over_minimum=reflux_ratio / minimum_reflux,
        theoretical_stages=stages,
        stages_above_minimum=stages - minimum_stages,
    )
