"""The heat-transfer time constant of a jacketed batch vessel, and the duty at a driving force.

A perfectly mixed batch held against a jacket at constant temperature approaches that jacket
exponentially:

    M·c_p·dT/dt = U·A·(T_j - T)      ⇒      (T - T_j)/(T₀ - T_j) = exp(-t/τ),  τ = M·c_p/(U·A)

`τ` is the number a chemist is usually after, because it is the one that scales badly: `M` grows
with the vessel volume and `A` grows with its surface, so `τ` grows roughly with the linear
dimension and a cool-down that took twenty minutes in the lab takes hours in a plant. That single
fact is most of why a route that worked at 1 L behaves differently at 250 L.

**`U` is an input and is never assumed.** The overall coefficient depends on the jacket service,
the agitation, the fouling and the wall — it is a vessel characterisation, measured, and a number
this server cannot derive. Chemclaw3's own probe set names an assumed `U` as the most dangerous
shape this question has, because the arithmetic downstream of it looks like engineering. So the
argument is required, with no default and no correlation behind it.

**What this is not.** There is no reaction heat here and no energy balance on a reaction: `Q` is
the *capacity* to move heat at a stated driving force, not a heat load. Putting a load against it
needs calorimetry, which is `servers/thermalsafety`'s input and not this server's. There is no
jacket-side dynamics, no fouling model, no heat loss to ambient, no phase change, and `U`, `c_p`
and the jacket temperature are all taken as constant over the transient.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from chemclaw_mcp_unitops.engine.validation import UnitOpsInputError, kelvin, positive

__all__ = ["APPROACH_AT_ONE_TIME_CONSTANT", "HeatTransferTransient", "time_constant"]

#: The fraction of the initial temperature gap closed at `t = τ`: `1 - 1/e`. Every first-order
#: system's defining number, and the one `engine/selftest.py` checks this module against.
APPROACH_AT_ONE_TIME_CONSTANT = 1.0 - math.exp(-1.0)


@dataclass(frozen=True)
class HeatTransferTransient:
    """A jacketed vessel's first-order thermal response, and the duty at the starting gap."""

    time_constant_seconds: float
    time_constant_minutes: float
    #: `U·A·(T₀ - T_j)` in watts, signed so that a cool-down is negative — the rate of heat
    #: transfer at the *initial* driving force, which is the largest it will be. It falls as the
    #: batch approaches the jacket, so this is a ceiling on the duty rather than an average.
    initial_duty_w: float
    #: The temperature after `time_seconds`, in °C, when one was asked for.
    temperature_after_c: float | None
    #: The time in seconds to reach `target_temperature_c`, when one was asked for.
    time_to_target_seconds: float | None
    time_to_target_minutes: float | None
    #: `1 - 1/e` = 0.632. Reported so the time constant is readable as what it is: at `t = τ` the
    #: batch has closed 63.2% of its gap to the jacket, not reached the jacket.
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
        batch_mass_kg: Mass of the batch contents, in kg.
        heat_capacity_j_per_kg_k: Specific heat capacity of the contents, in J/(kg·K). Water is
            4180; most organic solvents are 1600-2200.
        overall_heat_transfer_coefficient_w_per_m2_k: `U`, in W/(m²·K). A measured vessel
            characterisation. No default and no correlation: an assumed `U` is the whole of this
            answer.
        heat_transfer_area_m2: The **wetted** jacket area at this fill, in m². A part-filled vessel
            has less area than its jacket, and using the full jacket area overstates the duty.
        initial_temperature_c: The batch temperature at the start, in °C.
        jacket_temperature_c: The jacket service temperature, in °C, held constant.
        target_temperature_c: Optionally, a temperature to reach, in °C. Must lie strictly between
            the starting and the jacket temperature: the jacket temperature itself is reached only
            asymptotically, so asking for it is asking for infinite time.
        time_seconds: Optionally, a time to evaluate the temperature at, in seconds.

    Returns:
        The time constant, the duty at the initial gap, and whichever of the two inversions was
        asked for.

    Raises:
        UnitOpsInputError: If a mass, capacity, coefficient or area is not positive, if a
            temperature is below absolute zero, if the batch already sits at the jacket temperature
            (no gap, so no transient), or if a target lies outside the gap.
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
        if time_seconds < 0.0:
            raise UnitOpsInputError(f"the time must not be negative; got {time_seconds}.")
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
