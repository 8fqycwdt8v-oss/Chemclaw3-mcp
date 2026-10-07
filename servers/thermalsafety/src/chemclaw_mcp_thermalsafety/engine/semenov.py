"""The Semenov heat balance: the temperature at which cooling stops winning.

`runaway.py` covers no cooling at all; this covers a vessel or package that loses heat and is stable
until the exponential decomposition rate outruns linear heat loss. The ambient where that happens
drives storage and transport decisions.

Assumptions: uniform contents temperature, Newtonian loss `U·A·(T - T_ambient)`, single zero-order
Arrhenius kinetics, no reactant consumption. A large solid package needs Frank-Kamenetskii instead,
and an autocatalytic decomposition runs away below this answer. `sadt_semenov` is therefore not an
SADT, which is defined by UN Test Series H on a specific package; it is a planning estimate for
which test to book.
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

#: The search bracket in kelvin. A crossover outside it is reported, never clamped into a number.
_SEARCH_LOW_K = 233.15  # -40 °C
_SEARCH_HIGH_K = 673.15  # 400 °C

#: Bisection stopping width, K: far finer than the model's accuracy. Rounding an SADT up to 5 °C is
#: the caller's job.
_TOLERANCE_K = 1e-4


@dataclass(frozen=True)
class SemenovBalance:
    """Where heat generation overtakes heat loss, and the ambient that puts it there."""

    #: The ambient, °C, at which the balance is exactly tangential; above it the package self-heats
    #: without bound under this model.
    critical_ambient_c: float
    #: The contents' temperature, °C, at that tangency; above `critical_ambient_c` by `R·T²/Ea`.
    critical_contents_c: float
    #: The self-heat at tangency, K; distinguishes a few kelvin of margin from forty.
    self_heating_at_criticality_k: float
    #: The decomposition's heat output at tangency, W, for a sanity check against the calorimetry.
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

    `Q(T) = m · q_ref · exp(-Ea/R · (1/T - 1/T_ref))`: one measured rate plus Ea, which is what a
    DSC or ARC report gives. Extrapolating far below the reference is unsafe for an autocatalytic
    decomposition (an induction period is invisible).

    Raises:
        ThermalInputError: A non-positive mass, rate or activation energy, or a temperature at or
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

    At the critical condition the generation and loss curves touch, so values and slopes are equal:

        Q(T_c) = U·A·(T_c - T_a)          and          dQ/dT|_{T_c} = U·A

    Since `dQ/dT = Q·Ea/(R·T²)`, the second has one unknown and is solved by bisection (monotone in
    the bracket). Then `T_a = T_c - R·T_c²/Ea`: the critical self-heat is `R·T_c²/Ea`, so a high
    activation energy narrows the margin rather than reassuring.

    Raises:
        ThermalInputError: A non-positive quantity, a temperature at or below absolute zero, or a
            tangency outside the search bracket (stable everywhere relevant, or already past
            crossover at -40 °C).
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

    A reporting rule (*Manual of Tests and Criteria* §28.1.4.2), kept separate so the unrounded
    value — the real margin — stays visible.
    """
    return int(math.ceil(celsius / 5.0) * 5)
