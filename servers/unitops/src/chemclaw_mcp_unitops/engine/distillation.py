"""Fenske-Underwood-Gilliland: the shortcut column, for a binary key pair at constant volatility.

Three separate results that are always quoted together and are not the same kind of thing:

- **Fenske** gives `N_min`, the stages needed at *total* reflux. Exact algebra for a constant
  relative volatility, and the smallest stage count any column can achieve for that split.
- **Underwood** gives `R_min`, the reflux ratio below which the split is unreachable **at any**
  stage count. Also exact, by way of a root `θ` of

      Σ alpha_i·z_i/(alpha_i - θ) = 1 - q

  which this module finds by bisection on the interval `(1, alpha)` where the function is strictly
  increasing and changes sign, so the bracket is guaranteed and there is no failure mode to report.
- **Gilliland** gives `N` at a chosen `R`, and it is an **empirical correlation through a scatter
  of rigorous column calculations**, not a derivation. Molokanov's closed form of it is what is
  used here. The scatter it was drawn through is real: readings of ±10-20% on `N` are ordinary, and
  the correlation is at its worst exactly where a design sits, close to `R_min`.

**What this is not, and it is the whole warning.** A shortcut column is a screening arithmetic. It
is not a stage-by-stage simulation, it holds no VLE data, and it assumes the relative volatility is
one number over the whole column — which is false for anything azeotropic, anything strongly
non-ideal, and anything where the two ends are at very different temperatures. It returns
*theoretical* stages, including the reboiler, and says nothing about tray efficiency, column
diameter, pressure drop, hold-up, or how much of a solvent survives the swap in ppm. A chemist
asking "how many plates on our column" needs the theoretical count divided by a measured
efficiency, which is a number this server does not hold.

**The relative volatility is an input.** There is no VLE database here, and a boiling-point ratio
is not `alpha`. Supplying one is the chemist's job — from a measured x-y curve, from
a simulation, or from a literature value for the pair at the column's pressure.
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

#: A bound on the cost as much as a solver: this is the *most* halvings the loop may take, and
#: it stops earlier — once the interval is one float wide the midpoint is an endpoint, which is a
#: pole, so the loop breaks there. 200 is therefore a ceiling nothing reaches rather than a step
#: count; at 2^-200 it would take a bracket of width `alpha - 1` below 1e-60, far past the point a
#: double is exhausted — measured on a 95/5 split at alpha = 2.5, the loop breaks after **53**.
#: The same choice, for the same reason, as `servers/thermalsafety`'s Semenov root and
#: `servers/kinetics`'s CSTR inversion: an adaptive controller is what would want SciPy.
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
        relative_volatility: `alpha` of the light key over the heavy key, taken as constant over the
            column. Dimensionless and above 1.
        light_key_in_distillate: Mole fraction of the light key in the distillate, 0 to 1.
        light_key_in_bottoms: Mole fraction of the light key in the bottoms, 0 to 1.

    Returns:
        The minimum number of theoretical stages, including the reboiler as one stage.

    Raises:
        UnitOpsInputError: If `alpha` is at or below 1, a fraction is outside `(0, 1)`, or
            the bottoms are not leaner in the light key than the distillate.
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

    The first equation is solved for `θ` in `(1, alpha)`:

        alpha·z/(alpha - θ) + (1 - z)/(1 - θ) = 1 - q

    and the second evaluated at it:

        R_min + 1 = alpha·x_D/(alpha - θ) + (1 - x_D)/(1 - θ)

    The bracket is guaranteed: the left-hand side runs from -∞ at `θ → 1⁺` to +∞ at `θ → alpha⁻` and
    its derivative is positive throughout, so exactly one root lies between and bisection cannot
    fail to find it.

    Args:
        relative_volatility: `alpha`, dimensionless and above 1.
        light_key_in_feed: Mole fraction of the light key in the feed, 0 to 1.
        light_key_in_distillate: Mole fraction of the light key in the distillate, 0 to 1.
        feed_quality: `q`, the thermal condition of the feed: 1 for a saturated liquid (the usual
            case), 0 for a saturated vapour, above 1 for a subcooled liquid and below 0 for a
            superheated vapour. Defaulted to 1 because a feed pumped from a tank is a saturated or
            subcooled liquid, and because the alternative — refusing without it — would stop a
            chemist asking the question at all.

    Returns:
        `(R_min, θ)`. `θ` is returned because it is the number that says the root was found in the
        interval it must lie in, and it is meaningless outside `(1, alpha)`.

    Raises:
        UnitOpsInputError: If `alpha` is at or below 1, a composition is outside `(0, 1)`, `q` is
            not finite, or the distillate is not richer in the light key than the feed.
    """
    alpha = _volatility(relative_volatility)
    feed = fraction(light_key_in_feed, "the light key in the feed")
    top = fraction(light_key_in_distillate, "the light key in the distillate")
    # Checked here rather than left to the root: a NaN `q` makes every residual NaN, the bisection
    # walks to an endpoint, and the refusal below then blames the composition and the volatility.
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
        # The two endpoints are poles, not values: `residual(1)` and `residual(alpha)`
        # both divide by zero. The bracket closes on the root long before 200 halvings,
        # and once the interval is one float wide the midpoint *is* an endpoint — so the
        # loop stops there rather than evaluating a pole and reaching the model as an
        # opaque error id.
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

    **An empirical correlation, not a derivation.** Gilliland drew it through rigorous
    stage-by-stage results for a range of columns, and the scatter is real — ±10-20% on `N` is
    ordinary, and it is widest close to `R_min`, which is where a design sits.

    Args:
        minimum_stages: `N_min` from Fenske.
        minimum_reflux: `R_min` from Underwood.
        reflux_ratio: The `R = L/D` the column will actually be run at. Must exceed `R_min`:
            designs normally sit at 1.05 to 1.5 times it.

    Returns:
        The theoretical stages at that reflux, including the reboiler.

    Raises:
        UnitOpsInputError: If the reflux ratio is at or below the minimum — at `R_min` the stage
            count is infinite rather than large, and below it the split is unreachable at any stage
            count, so there is no number to return — or so close above it that the stage count
            overflows a float, or if it is not finite.
    """
    positive(minimum_stages, "the minimum stage count")
    positive(minimum_reflux, "the minimum reflux ratio")
    # Finite first: an explicit `inf` passed `<= minimum_reflux` and came back as an infinite
    # reflux beside NaN stage counts.
    if positive(reflux_ratio, "the reflux ratio") <= minimum_reflux:
        raise UnitOpsInputError(
            f"the reflux ratio of {reflux_ratio:.4g} is at or below the minimum of "
            f"{minimum_reflux:.4g}. At the minimum the column needs infinitely many stages, and "
            "below it this split cannot be reached at any stage count. Design reflux is normally "
            "1.05 to 1.5 times the minimum."
        )
    x = (reflux_ratio - minimum_reflux) / (reflux_ratio + 1.0)
    y = 1.0 - math.exp(((1.0 + 54.4 * x) / (11.0 + 117.2 * x)) * ((x - 1.0) / math.sqrt(x)))
    # **Above the minimum is not far enough above it.** For a small `X` the exponent is a large
    # negative number, `exp` underflows to 0 and `Y` is exactly 1.0 — measured at `R/R_min` of
    # 1.00001, where `(Y + N_min)/(1 - Y)` left as a bare `ZeroDivisionError`, an opaque error id
    # rather than a refusal. The stage count there is unbounded in floating point, which is the
    # same answer as at `R_min` itself, so it gets the same kind of sentence.
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
    #: Underwood's root. Between 1 and `alpha` by construction; reported so a reader can see the
    #: equation was solved rather than approximated.
    underwood_theta: float
    reflux_ratio: float
    #: `R/R_min`. The number a design is actually argued over — capital falls with stage count and
    #: utilities rise with reflux, and the optimum sits between 1.05 and 1.5 for most columns.
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
        reflux_ratio: The `R = L/D` to evaluate at. When omitted, `reflux_over_minimum` times the
            computed `R_min` is used instead — because `R_min` is not known until Underwood has run
            and a chemist asking this question rarely has a reflux ratio in mind yet.
        reflux_over_minimum: The multiple of `R_min` to use when no absolute reflux is given. 1.3
            is the middle of the band real columns are designed in; it is a **convention**, and the
            returned `reflux_ratio` says what was actually used.
        feed_quality: `q`; 1 for a saturated liquid.

    Returns:
        The minimum stages, the minimum reflux, the reflux actually used and the stages it needs.

    Raises:
        UnitOpsInputError: As the three functions above, and additionally when the three
            compositions do not order as `x_B < z < x_D` — the overall mass balance that neither
            Fenske nor Underwood sees the whole of.
    """
    # **The three compositions have to be orderable before any of the three correlations runs.**
    # Fenske checks `x_B < x_D` and Underwood checks `z < x_D`; between them nothing checked
    # `x_B < z`, and the overall balance `F·z = D·x_D + B·x_B` has no solution in positive `D` and
    # `B` without it. Measured before this guard: alpha 2.5 with z = 0.30, x_D = 0.95 and x_B = 0.50
    # returned 7.26 theoretical stages for a split no column can produce — an answer, in the shape
    # of an answer, to a mass balance that does not close. Refusing beats approximating.
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
