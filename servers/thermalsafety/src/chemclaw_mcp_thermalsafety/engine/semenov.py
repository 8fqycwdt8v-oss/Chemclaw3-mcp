"""The Semenov heat balance: the temperature at which cooling stops winning.

`runaway.py` answers "what happens with **no** cooling at all" — an adiabatic rise, a TMR_ad, a
criticality class. This module answers the other half: a vessel or a package that *does* lose heat
to its surroundings is stable until the exponential growth of the decomposition rate outruns the
linear growth of the heat loss, and the ambient temperature where that crossover happens is the one
a storage or transport decision turns on.

**What the Semenov model assumes, stated because every one of them is a way to get the wrong
answer.** The contents are at a uniform temperature (well-stirred liquid, or a solid small enough
that internal conduction is not the limit); the heat loss is Newtonian, `U·A·(T - T_ambient)`; the
kinetics are a single zero-order Arrhenius step over the region of interest; and no reactant is
consumed. Each is conservative in some regimes and optimistic in others — a large solid package is
conduction-limited and needs Frank-Kamenetskii instead, and a real decomposition that autocatalyses
runs away below what this returns.

**So `sadt_semenov` is not an SADT.** A Self-Accelerating Decomposition Temperature is *defined* by
UN Test Series H — H.1 (the United States SADT test), H.2, H.3 or H.4 — on a **specific package** in
a specific size, and a number computed from a heat balance is an estimate for planning which test to
book and at what temperature to start. The function is named for the model rather than for the
regulation on purpose, and the tool that wraps it says the same thing to the model in its first
line. Shipping this as `sadt` would be shipping a regulatory determination as arithmetic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from chemclaw_mcp_thermalsafety.engine.runaway import (
    ABSOLUTE_ZERO_C,
    GAS_CONSTANT_J_PER_MOL_K,
    ThermalInputError,
    _kelvin,
    _positive,
)

#: The bracket the crossover is searched in, in kelvin. The lower bound is below any storage
#: ambient anybody transports at; the upper is above the temperature at which "stable" has stopped
#: being the question. A crossover outside it is *reported* rather than clamped — a clamped root
#: would be returned as a number and read as one.
_SEARCH_LOW_K = 233.15  # -40 °C
_SEARCH_HIGH_K = 673.15  # 400 °C

#: The bisection's stopping width, in kelvin. Far finer than the model's own accuracy, so that the
#: reported value is limited by the physics rather than by the search — and the UN convention of
#: rounding an SADT *up* to the next 5 °C is applied by the caller, not hidden in here.
_TOLERANCE_K = 1e-4


@dataclass(frozen=True)
class SemenovBalance:
    """Where heat generation overtakes heat loss, and the ambient that puts it there."""

    #: The ambient temperature, °C, at which the balance is exactly tangential — above this the
    #: package self-heats without bound under this model.
    critical_ambient_c: float
    #: The contents' temperature, °C, at that tangency. Always above `critical_ambient_c`; the gap
    #: is `R·T²/Ea` and is the model's whole content in one number.
    critical_contents_c: float
    #: The self-heat at tangency, K — the steady-state excess the contents run at just before the
    #: balance is lost. Returned because a gap of a few kelvin and a gap of forty are very different
    #: situations that the critical ambient alone does not distinguish.
    self_heating_at_criticality_k: float
    #: The heat the decomposition releases at tangency, W. Returned for the same reason the balance
    #: is: it is what the loss term has to match, and a chemist can sanity-check it against the
    #: calorimetry the inputs came from.
    heat_generation_at_criticality_w: float


def heat_generation_w(
    *,
    temperature_c: float,
    mass_kg: float,
    heat_release_rate_w_per_kg: float,
    reference_temperature_c: float,
    activation_energy_kj_per_mol: float,
) -> float:
    """The decomposition's heat output at a temperature, W, from one measured rate.

    Zero-order Arrhenius extrapolation of a single measured point:
    `Q(T) = m · q_ref · exp(-Ea/R · (1/T - 1/T_ref))`. One measured rate plus an activation energy
    is what a DSC or ARC report actually gives, which is why the signature takes that pair rather
    than a pre-exponential factor nobody has to hand.

    **Extrapolating far below the measured point is where this is wrong**, and it is wrong in the
    unsafe direction for an autocatalytic decomposition: the measured rate at 200 °C says nothing
    about an induction period at 40 °C. The caller states the reference; this function will not
    guess one.

    Raises:
        ThermalInputError: a non-positive mass, rate or activation energy, or a temperature at or
            below absolute zero.
    """
    temperature_k = _kelvin(temperature_c, name="temperature")
    reference_k = _kelvin(reference_temperature_c, name="reference_temperature")
    mass = _positive(mass_kg, name="mass_kg", unit="kg")
    rate = _positive(heat_release_rate_w_per_kg, name="heat_release_rate_w_per_kg", unit="W/kg")
    activation = _positive(
        activation_energy_kj_per_mol, name="activation_energy_kj_per_mol", unit="kJ/mol"
    )
    exponent = -(activation * 1000.0 / GAS_CONSTANT_J_PER_MOL_K) * (
        1.0 / temperature_k - 1.0 / reference_k
    )
    return mass * rate * math.exp(exponent)


def semenov_criticality(
    *,
    mass_kg: float,
    heat_release_rate_w_per_kg: float,
    reference_temperature_c: float,
    activation_energy_kj_per_mol: float,
    heat_transfer_coefficient_w_per_m2_k: float,
    surface_area_m2: float,
) -> SemenovBalance:
    """The tangency of the Semenov balance, and the ambient temperature that produces it.

    At the critical condition the generation and loss curves touch, so their values **and** their
    slopes are equal:

        Q(T_c) = U·A·(T_c - T_a)          and          dQ/dT|_{T_c} = U·A

    The second equation has one unknown, `T_c`, because `dQ/dT = Q·Ea/(R·T²)` — it is solved by
    bisection over `_SEARCH_LOW_K`..`_SEARCH_HIGH_K`, where the function is monotone because `Q`
    grows faster than `T²`. Substituting back into the first gives the whole model in one line:

        T_a = T_c - R·T_c²/Ea

    so the critical *self-heating* is `R·T_c²/Ea` and nothing else. That is why a high activation
    energy is dangerous here rather than reassuring: it makes the tolerable self-heat smaller, so a
    package sits closer to its own crossover than the same heat release at a lower Ea would.

    Raises:
        ThermalInputError: any non-positive quantity, a temperature at or below absolute zero, or a
            tangency outside the search bracket — which means the package is either stable at every
            temperature this model is worth running at, or already past crossover at -40 °C. Both
            are reported rather than returned as a clamped number.
    """
    conductance = _positive(
        heat_transfer_coefficient_w_per_m2_k,
        name="heat_transfer_coefficient_w_per_m2_k",
        unit="W/(m²·K)",
    ) * _positive(surface_area_m2, name="surface_area_m2", unit="m²")
    activation_j = (
        _positive(activation_energy_kj_per_mol, name="activation_energy_kj_per_mol", unit="kJ/mol")
        * 1000.0
    )

    def slope_excess(temperature_k: float) -> float:
        """`dQ/dT - U·A` — negative while the loss still wins, positive once it does not."""
        generation = heat_generation_w(
            temperature_c=temperature_k + ABSOLUTE_ZERO_C,
            mass_kg=mass_kg,
            heat_release_rate_w_per_kg=heat_release_rate_w_per_kg,
            reference_temperature_c=reference_temperature_c,
            activation_energy_kj_per_mol=activation_energy_kj_per_mol,
        )
        slope = generation * activation_j / (GAS_CONSTANT_J_PER_MOL_K * temperature_k**2)
        return slope - conductance

    low, high = _SEARCH_LOW_K, _SEARCH_HIGH_K
    if slope_excess(low) >= 0:
        raise ThermalInputError(
            f"the generation curve is already steeper than the {conductance:.4g} W/K heat loss at "
            f"{low + ABSOLUTE_ZERO_C:.0f} °C, so this package has no stable ambient in the range "
            "this model covers — the inputs describe something that self-heats in a freezer, which "
            "is usually a rate or an activation energy in the wrong unit"
        )
    if slope_excess(high) <= 0:
        raise ThermalInputError(
            f"the {conductance:.4g} W/K heat loss still outruns the generation at "
            f"{high + ABSOLUTE_ZERO_C:.0f} °C, so no crossover exists below it; the decomposition "
            "is too slow or the package too well cooled for a Semenov estimate to say anything"
        )
    while high - low > _TOLERANCE_K:
        middle = (low + high) / 2.0
        if slope_excess(middle) < 0:
            low = middle
        else:
            high = middle
    contents_k = (low + high) / 2.0

    self_heating = GAS_CONSTANT_J_PER_MOL_K * contents_k**2 / activation_j
    ambient_k = contents_k - self_heating
    if ambient_k <= 0:
        raise ThermalInputError(
            f"the critical self-heating is {self_heating:.0f} K, which puts the critical ambient "
            "below absolute zero; the activation energy is too low for a Semenov balance to be "
            "meaningful at this heat release"
        )
    return SemenovBalance(
        critical_ambient_c=ambient_k + ABSOLUTE_ZERO_C,
        critical_contents_c=contents_k + ABSOLUTE_ZERO_C,
        self_heating_at_criticality_k=self_heating,
        heat_generation_at_criticality_w=heat_generation_w(
            temperature_c=contents_k + ABSOLUTE_ZERO_C,
            mass_kg=mass_kg,
            heat_release_rate_w_per_kg=heat_release_rate_w_per_kg,
            reference_temperature_c=reference_temperature_c,
            activation_energy_kj_per_mol=activation_energy_kj_per_mol,
        ),
    )


def round_up_to_nearest_five(celsius: float) -> int:
    """The UN convention for reporting an SADT: round **up** to the next whole 5 °C.

    Separate from `semenov_criticality` on purpose. The model produces a continuous number; this is
    a reporting rule from the *Manual of Tests and Criteria* (§28.1.4.2), and folding it into the
    physics would make it impossible for a caller to see the unrounded value — which is the one that
    says how much margin there is.
    """
    return int(math.ceil(celsius / 5.0) * 5)
