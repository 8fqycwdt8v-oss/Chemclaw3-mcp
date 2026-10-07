"""Fenske, Underwood and Gilliland against values computed by hand from the published forms.

Case: alpha = 2.5, 95/5 split, equimolar saturated-liquid feed. A fixture recorded from the code
would only assert the code has not changed. The hand arithmetic:

- Fenske: `ln[(0.95/0.05)·(0.95/0.05)] / ln 2.5 = ln 361 / 0.91629 = 6.4269`.
- Underwood, q = 1: `1.25/(2.5-θ) + 0.5/(1-θ) = 0` gives `θ = 1.42857`; then
  `R_min + 1 = 2.5·0.95/(2.5-1.42857) + 0.05/(1-1.42857) = 2.1`, so `R_min = 1.100`, matching
  the binary shortcut `[x_D/z - alpha(1-x_D)/(1-z)]/(alpha-1) = (1.9 - 0.25)/1.5`.
- Gilliland at `R = 1.3·R_min = 1.43`: `X = 0.33/2.43 = 0.135802`,
  `Y = 1 - exp[(8.3876/26.9161)·(-0.86420/0.368513)] = 0.518458`, and
  `N = (0.518458 + 6.4269)/0.481542 = 14.42`.
"""

from __future__ import annotations

import math
import re

import pytest
from chemclaw_mcp_unitops.engine import distillation
from chemclaw_mcp_unitops.engine.validation import UnitOpsInputError

COLUMN = {
    "relative_volatility": 2.5,
    "light_key_in_feed": 0.5,
    "light_key_in_distillate": 0.95,
    "light_key_in_bottoms": 0.05,
}


def test_fenske_reproduces_the_hand_computed_minimum_stages() -> None:
    """6.4269 stages at total reflux, from `ln 361 / ln 2.5`."""
    found = distillation.fenske_minimum_stages(
        relative_volatility=2.5, light_key_in_distillate=0.95, light_key_in_bottoms=0.05
    )
    assert found == pytest.approx(6.4269, abs=5.0e-5)


def test_fenske_is_symmetric_in_the_two_end_specifications() -> None:
    """Swapping the two end purities cannot change N_min.

    A property of the Fenske group rather than of either number, so it catches an argument swapped
    at the call site.
    """
    one = distillation.fenske_minimum_stages(
        relative_volatility=2.5, light_key_in_distillate=0.99, light_key_in_bottoms=0.30
    )
    other = distillation.fenske_minimum_stages(
        relative_volatility=2.5, light_key_in_distillate=0.70, light_key_in_bottoms=0.01
    )
    assert one == pytest.approx(other, rel=1e-12)


def test_underwood_reproduces_the_hand_computed_root_and_minimum_reflux() -> None:
    """θ = 1.42857 and R_min = 1.100, both written down before this module existed."""
    minimum_reflux, theta = distillation.underwood_minimum_reflux(
        relative_volatility=2.5, light_key_in_feed=0.5, light_key_in_distillate=0.95
    )
    assert theta == pytest.approx(1.42857, abs=1.0e-5)
    assert minimum_reflux == pytest.approx(1.100, abs=1.0e-5)


def test_the_root_satisfies_the_equation_it_was_found_from() -> None:
    """The residual at θ is zero: θ *is* a root of Underwood's first equation.

    Comparing θ to a number would pass a bisection that stopped on the wrong side of a pole.
    """
    alpha, feed = 2.5, 0.5
    _, theta = distillation.underwood_minimum_reflux(
        relative_volatility=alpha, light_key_in_feed=feed, light_key_in_distillate=0.95
    )
    residual = alpha * feed / (alpha - theta) + (1.0 - feed) / (1.0 - theta)
    assert residual == pytest.approx(0.0, abs=1.0e-12)
    assert 1.0 < theta < alpha


@pytest.mark.parametrize(
    ("alpha", "feed", "distillate"),
    [(2.5, 0.5, 0.95), (1.8, 0.35, 0.99), (4.0, 0.6, 0.90), (1.15, 0.5, 0.98)],
)
def test_the_bisected_minimum_reflux_matches_the_closed_form_binary_shortcut(
    alpha: float, feed: float, distillate: float
) -> None:
    """Two routes to one number, and the closed form appears nowhere in the module."""
    found, _ = distillation.underwood_minimum_reflux(
        relative_volatility=alpha, light_key_in_feed=feed, light_key_in_distillate=distillate
    )
    closed = (distillate / feed - alpha * (1.0 - distillate) / (1.0 - feed)) / (alpha - 1.0)
    assert found == pytest.approx(closed, rel=1.0e-12)


@pytest.mark.parametrize(
    ("alpha", "feed"),
    [(2.5, 1.0e-9), (2.5, 1.0e-15), (1.001, 0.5), (1.000000000001, 0.5)],
)
def test_a_root_driven_towards_a_pole_answers_rather_than_dividing_by_zero(
    alpha: float, feed: float
) -> None:
    """A root driven towards a pole answers rather than dividing by zero.

    A feed almost free of light key pushes θ towards `alpha`; a volatility near 1 squeezes the
    interval onto 1. The root stays strictly inside `(1, alpha)` and R_min is a very large finite
    number, the right answer for an unbuildable column; a `ZeroDivisionError` would reach the model
    as an opaque error id.
    """
    minimum_reflux, theta = distillation.underwood_minimum_reflux(
        relative_volatility=alpha, light_key_in_feed=feed, light_key_in_distillate=0.95
    )
    assert 1.0 < theta < alpha
    assert math.isfinite(minimum_reflux)
    assert minimum_reflux > 100.0


def test_a_vapour_feed_needs_more_reflux_than_a_liquid_one() -> None:
    """A vapour feed needs more reflux than a liquid one.

    This proves `q` reaches the answer in the physical direction; an accepted-but-ignored `q` would
    give identical numbers.
    """
    liquid, _ = distillation.underwood_minimum_reflux(
        relative_volatility=2.5,
        light_key_in_feed=0.5,
        light_key_in_distillate=0.95,
        feed_quality=1.0,
    )
    vapour, _ = distillation.underwood_minimum_reflux(
        relative_volatility=2.5,
        light_key_in_feed=0.5,
        light_key_in_distillate=0.95,
        feed_quality=0.0,
    )
    assert vapour > liquid


def test_gilliland_reproduces_the_hand_computed_stage_count() -> None:
    """14.42 stages at 1.3 times the minimum reflux, from Molokanov's closed form by hand."""
    found = distillation.gilliland_stages(
        minimum_stages=6.4269, minimum_reflux=1.100, reflux_ratio=1.3 * 1.100
    )
    assert found == pytest.approx(14.42, abs=0.01)


def test_the_whole_shortcut_agrees_with_the_three_pieces_computed_separately() -> None:
    """`shortcut_column` composes; it must not re-derive."""
    column = distillation.shortcut_column(**COLUMN)
    assert column.minimum_stages == pytest.approx(6.4269, abs=5.0e-5)
    assert column.minimum_reflux_ratio == pytest.approx(1.100, abs=1.0e-5)
    assert column.reflux_over_minimum == pytest.approx(1.3, rel=1.0e-12)
    assert column.theoretical_stages == pytest.approx(14.42, abs=0.01)
    assert column.stages_above_minimum == pytest.approx(
        column.theoretical_stages - column.minimum_stages, rel=1.0e-12
    )


def test_an_explicit_reflux_ratio_overrides_the_convention() -> None:
    """The 1.3 default is a convention, and the answer says which reflux it actually used."""
    column = distillation.shortcut_column(**COLUMN, reflux_ratio=2.2)
    assert column.reflux_ratio == pytest.approx(2.2)
    assert column.reflux_over_minimum == pytest.approx(2.2 / 1.100, rel=1.0e-4)
    assert column.theoretical_stages < distillation.shortcut_column(**COLUMN).theoretical_stages


def test_total_reflux_converges_on_fenske() -> None:
    """The relation the readiness probe rests on, asserted here as a limit rather than a point."""
    minimum_stages, minimum_reflux = 6.4269, 1.100
    previous = math.inf
    for multiple in (10.0, 1.0e3, 1.0e5, 1.0e7):
        stages = distillation.gilliland_stages(
            minimum_stages=minimum_stages,
            minimum_reflux=minimum_reflux,
            reflux_ratio=multiple * minimum_reflux,
        )
        assert stages < previous
        assert stages > minimum_stages
        previous = stages
    assert previous == pytest.approx(minimum_stages, rel=1.0e-6)


def test_an_azeotrope_is_refused_by_name_rather_than_divided_by_zero() -> None:
    """alpha = 1 is `ln 1` in the denominator, and the message has to say what that means."""
    with pytest.raises(UnitOpsInputError, match="azeotrope"):
        distillation.fenske_minimum_stages(
            relative_volatility=1.0, light_key_in_distillate=0.95, light_key_in_bottoms=0.05
        )


def test_keys_the_wrong_way_round_are_refused_rather_than_returned_negative() -> None:
    """A bottoms richer in the light key than the distillate gives a negative `N_min` silently."""
    with pytest.raises(UnitOpsInputError, match="keys are swapped"):
        distillation.fenske_minimum_stages(
            relative_volatility=2.5, light_key_in_distillate=0.05, light_key_in_bottoms=0.95
        )


def test_a_distillate_no_richer_than_its_feed_is_refused() -> None:
    """Underwood's second equation would answer; the column would not exist."""
    with pytest.raises(UnitOpsInputError, match="not enriching"):
        distillation.underwood_minimum_reflux(
            relative_volatility=2.5, light_key_in_feed=0.95, light_key_in_distillate=0.90
        )


def test_a_reflux_at_or_below_the_minimum_is_refused_with_the_design_band_named() -> None:
    """At `R_min` the stage count is infinite, so a finite answer there would be a lie."""
    with pytest.raises(UnitOpsInputError, match=re.escape("1.05 to 1.5")):
        distillation.gilliland_stages(
            minimum_stages=6.4269, minimum_reflux=1.100, reflux_ratio=1.100
        )
    with pytest.raises(UnitOpsInputError):
        distillation.gilliland_stages(minimum_stages=6.4269, minimum_reflux=1.100, reflux_ratio=0.9)


def test_a_percentage_entered_as_a_fraction_is_refused_with_the_fix_in_the_message() -> None:
    """95 is not 0.95, and the tool says so rather than failing on a logarithm."""
    with pytest.raises(UnitOpsInputError, match=re.escape("95% is 0.95")):
        distillation.fenske_minimum_stages(
            relative_volatility=2.5, light_key_in_distillate=95.0, light_key_in_bottoms=5.0
        )


def test_a_split_whose_overall_mass_balance_cannot_close_is_refused() -> None:
    """A split whose overall mass balance cannot close is refused.

    Fenske sees distillate against bottoms and Underwood distillate against feed; neither compares
    bottoms with feed, and `F·z = D·x_D + B·x_B` has no positive solution when both products are
    richer than the feed. Without this an unbuildable column gets an ordinary-looking stage count.
    """
    with pytest.raises(UnitOpsInputError, match=re.escape("F·z = D·x_D + B·x_B")):
        distillation.shortcut_column(
            relative_volatility=2.5,
            light_key_in_feed=0.30,
            light_key_in_distillate=0.95,
            light_key_in_bottoms=0.50,
        )


def test_bottoms_exactly_at_the_feed_composition_are_refused_too() -> None:
    """The boundary, where the balance needs a bottoms flow of exactly zero.

    A column that sends the whole feed overhead is a total vaporiser, not a separation, and the
    strict inequality is what keeps this from reading as a very difficult split.
    """
    with pytest.raises(UnitOpsInputError):
        distillation.shortcut_column(
            relative_volatility=2.5,
            light_key_in_feed=0.30,
            light_key_in_distillate=0.95,
            light_key_in_bottoms=0.30,
        )


def test_an_ordinary_split_is_untouched_by_the_balance_guard() -> None:
    """The balance guard leaves an ordinary demanding split untouched.

    Asserted beside the refusals so "refuses everything" cannot pass as "has a guard".
    """
    column = distillation.shortcut_column(
        relative_volatility=2.5,
        light_key_in_feed=0.40,
        light_key_in_distillate=0.99,
        light_key_in_bottoms=0.01,
    )
    assert column.theoretical_stages > column.minimum_stages


@pytest.mark.parametrize("multiple", [1.00001, 1.0 + 1.0e-6, 1.0 + 1.0e-9])
def test_a_reflux_a_hair_above_the_minimum_is_refused_rather_than_divided_by_zero(
    multiple: float,
) -> None:
    """A reflux a hair above the minimum is refused rather than divided by zero.

    Just above `R_min` the Gilliland exponent underflows, `Y` is 1 and `1 - Y` is zero. Covered via
    both the default convention and an explicit ratio.
    """
    with pytest.raises(UnitOpsInputError, match=re.escape("1.05 to 1.5")):
        distillation.shortcut_column(**COLUMN, reflux_over_minimum=multiple)
    minimum_reflux = distillation.shortcut_column(**COLUMN).minimum_reflux_ratio
    with pytest.raises(UnitOpsInputError, match="unbounded"):
        distillation.shortcut_column(**COLUMN, reflux_ratio=multiple * minimum_reflux)


@pytest.mark.parametrize("bad", [math.inf, math.nan])
def test_a_non_finite_explicit_reflux_ratio_is_refused(bad: float) -> None:
    """`inf` used to come back as an infinite reflux beside NaN stage counts."""
    with pytest.raises(UnitOpsInputError, match="the reflux ratio must be a finite number"):
        distillation.shortcut_column(**COLUMN, reflux_ratio=bad)


@pytest.mark.parametrize("bad", [math.inf, -math.inf, math.nan])
def test_a_non_finite_feed_quality_is_refused_by_name(bad: float) -> None:
    """A NaN `q` used to walk Underwood's root onto an edge and blame the composition instead."""
    with pytest.raises(UnitOpsInputError, match="feed quality"):
        distillation.shortcut_column(**COLUMN, feed_quality=bad)
