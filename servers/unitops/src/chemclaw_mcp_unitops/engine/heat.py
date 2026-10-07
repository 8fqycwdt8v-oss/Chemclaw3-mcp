"""The heat-transfer time constant of a jacketed batch vessel, and the duty at a driving force.

    M·c_p·dT/dt = U·A·(T_j - T)      ⇒      (T - T_j)/(T₀ - T_j) = exp(-t/τ),  τ = M·c_p/(U·A)

`τ` grows roughly with the vessel's linear dimension (mass with volume, area with surface), which is
why lab cool-downs become plant hours. `U` is a required, measured vessel characterisation with no
default or correlation, since an assumed `U` makes everything downstream look like engineering. `Q`
is the capacity at a driving force, not a reaction heat load (that is `servers/thermalsafety`'s
calorimetry). No jacket dynamics, fouling, ambient loss or phase change; `U`, `c_p` and the jacket
temperature are constant.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from chemclaw_mcp_unitops.engine.validation import UnitOpsInputError, kelvin, non_negative, positive

__all__ = ["APPROACH_AT_ONE_TIME_CONSTANT", "HeatTransferTransient", "time_constant"]

#: The fraction of the initial gap closed at `t = τ`, `1 - 1/e`; `engine/selftest.py` checks against
#: it.
APPROACH_AT_ONE_TIME_CONSTANT = 1.0 - math.exp(-1.0)


@dataclass(frozen=True)
class HeatTransferTransient:
    """A jacketed vessel's first-order thermal response, and the duty at the starting gap."""

    time_constant_seconds: float
    time_constant_minutes: float
    #: `U·A·(T₀ - T_j)` in W, negative for a cool-down: the duty at the initial (largest) gap, so a
    #: ceiling rather than an average.
    initial_duty_w: float
    #: The temperature after `time_seconds`, in °C, when one was asked for.
    temperature_after_c: float | None
    #: The time in seconds to reach `target_temperature_c`, when one was asked for.
    time_to_target_seconds: float | None
    time_to_target_minutes: float | None
    #: `1 - 1/e` = 0.632: at `t = τ` the batch has closed 63.2% of its gap, not reached the jacket.
    approach_at_one_time_constant: float


def time_constant(
    *,
    batch_mass_kg: float,
    heat_capacity_j_per_kg_k: float,
    overall_heat_transfer_coefficient_w_per_m2_k: float,
    heat_transfer_area_m2: float,
    initial_temperature_c: float,
    jacket_temperature_c: float,
    target_temperature_c: float | None = None,
    time_seconds: float | None = None,
) -> HeatTransferTransient:
    """The first-order time constant of a jacketed batch, with the optional two inversions.

    Args:
        batch_mass_kg: Mass of the batch contents, kg.
        heat_capacity_j_per_kg_k: Specific heat capacity of the contents, J/(kg·K) (water 4180; most
            organic solvents 1600-2200).
        overall_heat_transfer_coefficient_w_per_m2_k: `U`, W/(m²·K), a measured vessel
            characterisation. No default.
        heat_transfer_area_m2: The **wetted** jacket area at this fill, m².
        initial_temperature_c: The batch temperature at the start, °C.
        jacket_temperature_c: The jacket service temperature, °C, held constant.
        target_temperature_c: Optionally a temperature to reach, °C, strictly between the start and
            the jacket (the jacket itself takes infinite time).
        time_seconds: Optionally, a time to evaluate the temperature at, in seconds.

    Returns:
        The time constant, the duty at the initial gap, and whichever inversion was asked for.

    Raises:
        UnitOpsInputError: A mass, capacity, coefficient or area is not positive, a temperature is
            below absolute zero, the batch already sits at the jacket temperature, or a target lies
            outside the gap.
    """
    positive(batch_mass_kg, "the batch mass")
    positive(heat_capacity_j_per_kg_k, "the heat capacity")
    positive(overall_heat_transfer_coefficient_w_per_m2_k, "the overall heat transfer coefficient")
    positive(heat_transfer_area_m2, "the heat transfer area")
    kelvin(initial_temperature_c, "the initial temperature")
    kelvin(jacket_temperature_c, "the jacket temperature")

    gap = initial_temperature_c - jacket_temperature_c
    if gap == 0.0:
        raise UnitOpsInputError(
            f"the batch and the jacket are both at {initial_temperature_c} °C, so there is no "
            "driving force and no transient to describe. Give the jacket the temperature you would "
            "actually run it at."
        )

    duty_area = overall_heat_transfer_coefficient_w_per_m2_k * heat_transfer_area_m2
    tau = batch_mass_kg * heat_capacity_j_per_kg_k / duty_area

    after: float | None = None
    if time_seconds is not None:
        # `non_negative` rather than `< 0.0`, which is False for NaN.
        non_negative(time_seconds, "the time")
        after = jacket_temperature_c + gap * math.exp(-time_seconds / tau)

    to_target: float | None = None
    if target_temperature_c is not None:
        kelvin(target_temperature_c, "the target temperature")
        remaining = target_temperature_c - jacket_temperature_c
        if remaining / gap >= 1.0:
            raise UnitOpsInputError(
                f"the target of {target_temperature_c} °C is not between the starting temperature "
                f"({initial_temperature_c} °C) and the jacket ({jacket_temperature_c} °C), so this "
                "jacket never reaches it. A jacket can only carry a batch towards itself."
            )
        if remaining / gap <= 0.0:
            raise UnitOpsInputError(
                f"the target of {target_temperature_c} °C is at or past the jacket temperature "
                f"({jacket_temperature_c} °C). A first-order approach reaches the jacket only "
                "asymptotically, so that time is infinite rather than large."
            )
        to_target = -tau * math.log(remaining / gap)

    return HeatTransferTransient(
        time_constant_seconds=tau,
        time_constant_minutes=tau / 60.0,
        initial_duty_w=-duty_area * gap,
        temperature_after_c=after,
        time_to_target_seconds=to_target,
        time_to_target_minutes=None if to_target is None else to_target / 60.0,
        approach_at_one_time_constant=APPROACH_AT_ONE_TIME_CONSTANT,
    )
