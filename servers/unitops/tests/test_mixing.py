"""Agitation power, tip speed and Zwietering's `N_js`, against relations written independently.

Two of the three are definitions, so they are checked against arithmetic done by hand. The third —
Zwietering — has no closed form to check against, and the honest substitute is not a recorded
fixture but the two things a published correlation must satisfy whatever its constants are: it has
to be **dimensionally homogeneous**, and it has to respond to each input with the exponent that was
published. Both are checked here, and neither can be satisfied by the module agreeing with itself.

What could **not** be validated here, stated rather than implied: there is no worked Zwietering
example in this repository to compare a number against, and the geometry constant `S` is supplied
by the caller. So what is asserted is the correlation's structure and its scale-up consequence, not
an absolute speed. `servers/unitops/README.md` says the same in the place a reader will look.
"""

from __future__ import annotations

import math

import pytest
from chemclaw_mcp_unitops.engine import mixing
from chemclaw_mcp_unitops.engine.validation import UnitOpsInputError

SLURRY = {
    "impeller_diameter_m": 0.45,
    "particle_diameter_m": 1.5e-4,
    "particle_density_kg_per_m3": 1350.0,
    "liquid_density_kg_per_m3": 880.0,
    "liquid_viscosity_pa_s": 1.2e-3,
    "solids_loading_percent": 12.0,
    "zwietering_constant": 6.0,
    "power_number": 1.5,
    "liquid_volume_m3": 0.25,
}


def test_the_power_and_tip_speed_are_the_hand_computed_ones() -> None:
    """`P = N_p·rho·N³·D⁵` and `π N D`, at 300 rpm on a 0.45 m impeller in 880 kg/m³ liquid.

    By hand: N = 5 rev/s, so P = 1.5 x 880 x 125 x 0.45⁵ = 1.5 x 880 x 125 x 0.0184528 = 3044.7 W,
    and the tip speed is π x 5 x 0.45 = 7.0686 m/s.
    """
    duty = mixing.agitation_scale_up(
        small_impeller_diameter_m=0.45,
        small_speed_rpm=300.0,
        small_liquid_volume_m3=0.25,
        large_impeller_diameter_m=0.90,
        large_liquid_volume_m3=2.0,
        power_number=1.5,
        liquid_density_kg_per_m3=880.0,
        liquid_viscosity_pa_s=1.2e-3,
    ).small
    assert duty.power_w == pytest.approx(3044.7, rel=1.0e-4)
    assert duty.tip_speed_m_per_s == pytest.approx(7.0686, rel=1.0e-4)
    assert duty.power_per_volume_w_per_m3 == pytest.approx(3044.7 / 0.25, rel=1.0e-4)
    # Re = 880 x 5 x 0.45² / 0.0012 = 742 500.
    assert duty.reynolds_number == pytest.approx(742500.0, rel=1.0e-4)
    assert duty.turbulent is True


def test_a_viscous_liquid_is_reported_as_outside_the_constant_power_number_regime() -> None:
    """The power arithmetic still answers; the answer says its own assumption has failed.

    Refusing would be wrong — a viscous slurry is a real question — but returning the number with
    no qualification is how a constant `N_p` gets used at Re = 300.
    """
    duty = mixing.agitation_scale_up(
        small_impeller_diameter_m=0.05,
        small_speed_rpm=60.0,
        small_liquid_volume_m3=1.0e-3,
        large_impeller_diameter_m=0.45,
        large_liquid_volume_m3=0.25,
        power_number=1.5,
        liquid_density_kg_per_m3=1100.0,
        liquid_viscosity_pa_s=5.0,
    ).small
    assert duty.reynolds_number < mixing.TURBULENT_REYNOLDS
    assert duty.turbulent is False


def test_matching_power_per_volume_reduces_to_the_similarity_rule() -> None:
    """Between geometrically similar vessels, `N₂ = N₁(D₁/D₂)^(2/3)` — a rule not in the module."""
    small_diameter, large_diameter, volume, speed = 0.10, 0.60, 1.0e-3, 500.0
    scaled = mixing.agitation_scale_up(
        small_impeller_diameter_m=small_diameter,
        small_speed_rpm=speed,
        small_liquid_volume_m3=volume,
        large_impeller_diameter_m=large_diameter,
        large_liquid_volume_m3=volume * (large_diameter / small_diameter) ** 3,
        power_number=5.0,
        liquid_density_kg_per_m3=1000.0,
        liquid_viscosity_pa_s=1.0e-3,
    )
    assert scaled.matched_power_per_volume.speed_rpm == pytest.approx(
        speed * (small_diameter / large_diameter) ** (2.0 / 3.0), rel=1.0e-12
    )
    assert scaled.matched_power_per_volume.power_per_volume_w_per_m3 == pytest.approx(
        scaled.small.power_per_volume_w_per_m3, rel=1.0e-12
    )


def test_a_vessel_that_is_not_geometrically_similar_does_not_get_the_similarity_answer() -> None:
    """The reason the module solves from the supplied volumes rather than from `(D₁/D₂)^(2/3)`.

    A 1 L round-bottomed flask and a 250 L vessel are not similar, and assuming they were is a
    silent error. Here the real volumes are 1 L and 250 L on impellers of 0.05 m and 0.45 m, which
    is a P/V-matched speed the similarity rule misses by a factor this test pins as *different*
    rather than as a number, because the point is that the two answers are not the same.
    """
    scaled = mixing.agitation_scale_up(
        small_impeller_diameter_m=0.05,
        small_speed_rpm=500.0,
        small_liquid_volume_m3=1.0e-3,
        large_impeller_diameter_m=0.45,
        large_liquid_volume_m3=0.25,
        power_number=1.5,
        liquid_density_kg_per_m3=880.0,
        liquid_viscosity_pa_s=1.2e-3,
    )
    similar = 500.0 * (0.05 / 0.45) ** (2.0 / 3.0)
    assert scaled.matched_power_per_volume.speed_rpm != pytest.approx(similar, rel=1.0e-3)
    assert scaled.matched_power_per_volume.power_per_volume_w_per_m3 == pytest.approx(
        scaled.small.power_per_volume_w_per_m3, rel=1.0e-12
    )


def test_matching_tip_speed_is_exact_and_gives_away_power_per_volume() -> None:
    """The trade-off the tool exists to show: the two criteria cannot both be held."""
    scaled = mixing.agitation_scale_up(
        small_impeller_diameter_m=0.05,
        small_speed_rpm=500.0,
        small_liquid_volume_m3=1.0e-3,
        large_impeller_diameter_m=0.45,
        large_liquid_volume_m3=0.25,
        power_number=1.5,
        liquid_density_kg_per_m3=880.0,
        liquid_viscosity_pa_s=1.2e-3,
    )
    assert scaled.matched_tip_speed.tip_speed_m_per_s == pytest.approx(
        scaled.small.tip_speed_m_per_s, rel=1.0e-12
    )
    assert (
        scaled.matched_tip_speed.power_per_volume_w_per_m3 < scaled.small.power_per_volume_w_per_m3
    )
    assert scaled.matched_power_per_volume.tip_speed_m_per_s > scaled.small.tip_speed_m_per_s
    assert scaled.criteria_disagree_by == pytest.approx(
        scaled.matched_power_per_volume.speed_rpm / scaled.matched_tip_speed.speed_rpm, rel=1e-12
    )


def test_zwieterings_exponents_are_dimensionally_homogeneous() -> None:
    """The published set has to make a frequency, and that is arithmetic on the exponents alone.

    The kinematic viscosity nu is m²/s, `d_p` is m, the buoyancy group
    `g·(rho_s - rho_L)/rho_L` is m/s², `X` is dimensionless and `D` is m. So the metre
    exponents must cancel and the second exponents must come to -1. A transposed pair — 0.45 and
    0.2 swapped, say — returns a plausible speed and fails this.
    """
    exponents = mixing.ZWIETERING_EXPONENTS
    metres = (
        2.0 * exponents["kinematic_viscosity"]
        + exponents["particle_diameter"]
        + exponents["buoyancy"]
        + exponents["impeller_diameter"]
    )
    seconds = -exponents["kinematic_viscosity"] - 2.0 * exponents["buoyancy"]
    assert metres == pytest.approx(0.0, abs=1.0e-12)
    assert seconds == pytest.approx(-1.0, abs=1.0e-12)


@pytest.mark.parametrize(
    ("argument", "published"),
    [
        ("liquid_viscosity_pa_s", 0.1),
        ("particle_diameter_m", 0.2),
        ("solids_loading_percent", 0.13),
        ("impeller_diameter_m", -0.85),
    ],
)
def test_the_function_exhibits_each_published_exponent(argument: str, published: float) -> None:
    """A log-log slope off the function itself, against the exponent Zwietering published.

    This is the link the homogeneity check cannot make: the constants could be right and the
    expression could raise the wrong one to the wrong power.
    """
    step = 1.0e-5
    up = dict(SLURRY)
    up[argument] = SLURRY[argument] * (1.0 + step)
    down = dict(SLURRY)
    down[argument] = SLURRY[argument] * (1.0 - step)
    slope = math.log(
        mixing.just_suspended_speed(**up).speed_rev_per_s
        / mixing.just_suspended_speed(**down).speed_rev_per_s
    ) / math.log((1.0 + step) / (1.0 - step))
    assert slope == pytest.approx(published, abs=1.0e-6)


def test_the_buoyancy_group_carries_its_own_exponent() -> None:
    """Scaling the density difference alone, which is the only way to move the buoyancy
    group without moving anything else."""
    step = 1.0e-5
    difference = SLURRY["particle_density_kg_per_m3"] - SLURRY["liquid_density_kg_per_m3"]

    def at(scale: float) -> float:
        inputs = dict(SLURRY)
        inputs["particle_density_kg_per_m3"] = (
            SLURRY["liquid_density_kg_per_m3"] + scale * difference
        )
        return mixing.just_suspended_speed(**inputs).speed_rev_per_s

    slope = math.log(at(1.0 + step) / at(1.0 - step)) / math.log((1.0 + step) / (1.0 - step))
    assert slope == pytest.approx(0.45, abs=1.0e-6)


def test_suspending_the_same_slurry_costs_less_power_per_volume_at_the_larger_scale() -> None:
    """The literature consequence of `N_js ∝ D^-0.85`, and the reason equal P/V is conservative.

    At geometric similarity `P/V ∝ N³D²`, so at `N_js` it goes as `D^(-2.55+2) = D^-0.55`: the
    bigger vessel needs *less* power per unit volume to keep the same solid suspended. That is a
    standard scale-up statement, it is nowhere in this module, and a scale-up argued on equal P/V
    is therefore conservative for suspension.
    """
    small = dict(SLURRY)
    large = dict(SLURRY)
    ratio = 4.0
    large["impeller_diameter_m"] = SLURRY["impeller_diameter_m"] * ratio
    large["liquid_volume_m3"] = SLURRY["liquid_volume_m3"] * ratio**3
    at_small = mixing.just_suspended_speed(**small)
    at_large = mixing.just_suspended_speed(**large)
    assert at_large.power_per_volume_w_per_m3 < at_small.power_per_volume_w_per_m3
    assert at_large.power_per_volume_w_per_m3 / at_small.power_per_volume_w_per_m3 == pytest.approx(
        ratio**-0.55, rel=1.0e-9
    )


def test_a_neutrally_buoyant_solid_is_refused_rather_than_given_a_speed_of_zero() -> None:
    """A zero density difference makes the buoyancy group zero and `N_js` zero, which
    reads as "no agitation needed"."""
    inputs = dict(SLURRY)
    inputs["particle_density_kg_per_m3"] = inputs["liquid_density_kg_per_m3"]
    with pytest.raises(UnitOpsInputError, match="does not settle"):
        mixing.just_suspended_speed(**inputs)


def test_a_floating_solid_is_refused_for_the_same_reason() -> None:
    """A correlation fitted on settling sand says nothing about a solid that rises."""
    inputs = dict(SLURRY)
    inputs["particle_density_kg_per_m3"] = 700.0
    with pytest.raises(UnitOpsInputError, match="does not settle"):
        mixing.just_suspended_speed(**inputs)


def test_a_missing_geometry_constant_cannot_be_defaulted() -> None:
    """`S` has no default, so a caller without one gets a TypeError rather than a guess."""
    inputs = dict(SLURRY)
    del inputs["zwietering_constant"]
    with pytest.raises(TypeError):
        mixing.just_suspended_speed(**inputs)


def test_a_loading_outside_zwieterings_fitted_band_is_flagged_rather_than_answered_bare() -> None:
    """The half `turbulent` already had for the power number, and `N_js` did not have at all.

    **`X^0.13` is a weak exponent, so a loading wrong by a factor of 100 returns a plausible
    number.** Driven on this file's own `SLURRY`, a 10 wt% loading entered as `0.10` — the
    fraction/percentage confusion the argument's own description warns about in capitals — gives
    `N_js` 49.36 rpm against 89.82, **45.05% low**, and the P/V that follows from it 54.2 W/m³
    against 326.9, **83.40% low**. Both ratios are fixture-independent, because `X` enters the
    correlation as a bare factor: on `engine/selftest.py`'s slurry the same mistake gives 130.14 rpm
    against 236.81 and 267.4 W/m³ against 1611.5. That is an under-agitated vessel, and it is a
    speed somebody sets a drive to.
    Before this there was no refusal and no flag, and the docstring referred to "the loading range
    it was fitted over" without naming it.

    Reported rather than refused because the correlation is routinely used a little outside its
    regression; `within_fitted_range` is the same shape as `turbulent` for the same reason.
    """
    inside = mixing.just_suspended_speed(**{**SLURRY, "solids_loading_percent": 10.0})
    mistaken = mixing.just_suspended_speed(**{**SLURRY, "solids_loading_percent": 0.10})

    assert inside.within_fitted_range, "the fixture's own 10 wt% loading must be inside the band"
    assert inside.outside_fitted_range == (), "an in-band answer must carry no caveat"

    assert not mistaken.within_fitted_range, (
        f"a loading of 0.10 sits below the "
        f"{mixing.ZWIETERING_FITTED_RANGES['solids_loading_percent']} band and returned N_js = "
        f"{mistaken.speed_rpm:.2f} rpm with no flag at all"
    )
    assert len(mistaken.outside_fitted_range) == 1
    note = mistaken.outside_fitted_range[0]
    assert "below" in note and "0.1" in note, (
        f"the caveat does not name the input or the side: {note}"
    )

    # The measurement the docstring quotes, asserted rather than asserted-about.
    assert mistaken.speed_rpm / inside.speed_rpm == pytest.approx(1.0 - 0.4505, abs=5e-4), (
        "the understatement this flag exists for is not the one measured; if the correlation "
        "moved, the figures in the docstring moved with it"
    )


def test_a_particle_size_outside_the_fitted_band_is_flagged_on_its_own_axis() -> None:
    """The second band, because a flag that only ever fires on one input is one input's flag.

    `d_p` is in **metres** and a 200 µm crystal is `2.0e-4`, so the realistic mistake is entering
    `200` or `0.2`. Both land above the band. The in-band pair is checked at both ends as well, so a
    band accidentally narrowed to nothing would fail here rather than flag every call.
    """
    low, high = mixing.ZWIETERING_FITTED_RANGES["particle_diameter_m"]
    for diameter in (low, high, math.sqrt(low * high)):
        result = mixing.just_suspended_speed(**{**SLURRY, "particle_diameter_m": diameter})
        assert result.within_fitted_range, (
            f"d_p = {diameter:g} m is inside the band and was flagged"
        )

    for diameter in (0.2, high * 1.0001, low * 0.9999):
        result = mixing.just_suspended_speed(**{**SLURRY, "particle_diameter_m": diameter})
        assert not result.within_fitted_range, (
            f"d_p = {diameter:g} m is outside the band, unflagged"
        )
        note = next(one for one in result.outside_fitted_range if "d_p" in one)
        # Which side, because a caveat that says "below" for a value above the band sends a reader
        # to look for the wrong mistake — and 4-M6 of this guard's mutation set was exactly that.
        side = "below" if diameter < low else "above"
        wrong = "above" if side == "below" else "below"
        assert side in note and wrong not in note, (
            f"d_p = {diameter:g} m is {side} the band and the caveat says otherwise: {note}"
        )
