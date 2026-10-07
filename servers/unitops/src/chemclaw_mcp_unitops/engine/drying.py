"""Batch drying time: a constant-rate period, then a falling-rate period, from a drying curve.

The classical two-period model: above the critical moisture the surface stays wet and evaporation
runs at a constant rate `N_c`; below it the rate falls, here taken as linearly to zero:

    constant rate:  t₁ = m_s·(X₁ - X_c)/(A·N_c)
    falling rate:   t₂ = m_s·X_c/(A·N_c) · ln(X_c/X₂)

`m_s` is the bone-dry solid mass, `A` the drying surface, and `X` moisture on a **dry basis** (kg
moisture per kg dry solid: 20% wet basis is X = 0.25). Mixing up the basis errs by more than the 25%
gap, because X_c is subtracted from it; every argument names its basis.

Not a dryer model (`N_c` is measured on this material in this dryer), and assumes zero equilibrium
moisture (otherwise pass free moisture `X - X_e`). Not a specification: LOD, residual solvent and
desolvation are analytical questions. A diffusion-limited material dries more slowly than the linear
falling-rate leg predicts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from chemclaw_mcp_unitops.engine.validation import UnitOpsInputError, positive

__all__ = ["BatchDrying", "drying_time"]


@dataclass(frozen=True)
class BatchDrying:
    """A drying cycle split into its two periods."""

    constant_rate_seconds: float
    falling_rate_seconds: float
    total_time_seconds: float
    total_time_hours: float
    #: Moisture removed over the whole cycle, kg: the load on the condenser or vent.
    moisture_removed_kg: float
    #: True when the charge starts below the critical moisture, so the whole cycle is falling-rate.
    starts_in_the_falling_rate_period: bool


def drying_time(
    *,
    dry_solid_mass_kg: float,
    drying_area_m2: float,
    constant_rate_kg_per_m2_s: float,
    initial_moisture_dry_basis: float,
    critical_moisture_dry_basis: float,
    final_moisture_dry_basis: float,
) -> BatchDrying:
    """Time to take a batch from one moisture content to another, through both periods.

    A target at or above the critical moisture is answered within the constant-rate period (`m_s·(X₁
    - X₂)/(A·N_c)`), with `falling_rate_seconds` zero.

    Args:
        dry_solid_mass_kg: Mass of **bone-dry** solid, kg (a 100 kg cake at X = 0.25 holds 80 kg).
        drying_area_m2: Area available for evaporation, m²: the exposed surface, not the
        heat-transfer area. constant_rate_kg_per_m2_s: `N_c`, kg moisture per m² per second,
        measured from a drying curve on this material in this dryer. No default.
        initial_moisture_dry_basis: `X₁`, kg moisture per kg dry solid at the start. **Dry basis.**
        critical_moisture_dry_basis: `X_c`, where the rate starts to fall, kg per kg dry solid, from
        the same curve. final_moisture_dry_basis: `X₂`, the target, kg per kg dry solid; above zero,
        since zero takes infinite time in this model.

    Returns:
        The two periods, the total, and the moisture removed.

    Raises:
        UnitOpsInputError: A mass, area or rate is not positive, the final moisture is at or below
        zero, or the target is not below the start.
    """
    positive(dry_solid_mass_kg, "the dry solid mass")
    positive(drying_area_m2, "the drying area")
    positive(constant_rate_kg_per_m2_s, "the constant-period drying rate")
    positive(critical_moisture_dry_basis, "the critical moisture content")
    positive(initial_moisture_dry_basis, "the initial moisture content")
    positive(
        final_moisture_dry_basis,
        "the final moisture content, which cannot be zero because this model's rate falls linearly "
        "to zero at zero moisture and so reaches it only after infinite time;",
    )

    if final_moisture_dry_basis >= initial_moisture_dry_basis:
        raise UnitOpsInputError(
            f"the target moisture ({final_moisture_dry_basis} kg/kg) is not below the starting "
            f"moisture ({initial_moisture_dry_basis} kg/kg). Both are on a dry basis — kg of "
            "moisture per kg of BONE-DRY solid — so a wet-basis percentage entered here reads as a "
            "target wetter than the charge."
        )
    time_per_unit_moisture = dry_solid_mass_kg / (drying_area_m2 * constant_rate_kg_per_m2_s)
    starts_falling = initial_moisture_dry_basis <= critical_moisture_dry_basis

    if final_moisture_dry_basis >= critical_moisture_dry_basis:
        # The whole cycle is inside the constant-rate period; the falling-rate leg never starts.
        constant_seconds = time_per_unit_moisture * (
            initial_moisture_dry_basis - final_moisture_dry_basis
        )
        falling_seconds = 0.0
    elif starts_falling:
        constant_seconds = 0.0
        falling_seconds = (
            time_per_unit_moisture
            * critical_moisture_dry_basis
            * math.log(initial_moisture_dry_basis / final_moisture_dry_basis)
        )
    else:
        constant_seconds = time_per_unit_moisture * (
            initial_moisture_dry_basis - critical_moisture_dry_basis
        )
        falling_seconds = (
            time_per_unit_moisture
            * critical_moisture_dry_basis
            * math.log(critical_moisture_dry_basis / final_moisture_dry_basis)
        )

    total = constant_seconds + falling_seconds
    return BatchDrying(
        constant_rate_seconds=constant_seconds,
        falling_rate_seconds=falling_seconds,
        total_time_seconds=total,
        total_time_hours=total / 3600.0,
        moisture_removed_kg=dry_solid_mass_kg
        * (initial_moisture_dry_basis - final_moisture_dry_basis),
        starts_in_the_falling_rate_period=starts_falling,
    )
