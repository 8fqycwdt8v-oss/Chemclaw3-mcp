"""Batch drying time: a constant-rate period, then a falling-rate period, from a drying curve.

The classical two-period model. Above the critical moisture content the surface stays wet and
evaporation runs at a constant rate `N_c` set by heat and mass transfer to that surface; below it,
the rate falls because the surface can no longer be kept wet, and the usual first approximation is
that it falls **linearly to zero**:

    constant rate:  t₁ = m_s·(X₁ - X_c)/(A·N_c)
    falling rate:   t₂ = m_s·X_c/(A·N_c) · ln(X_c/X₂)

`m_s` is the bone-dry solid mass, `A` the drying surface and `X` the moisture content on a **dry
basis** — kg of moisture per kg of dry solid, so a cake that is 20% wet on a wet basis is
X = 0.25, not 0.2. Getting that backwards is the mistake this module refuses hardest to make
quietly, which is why every argument says which basis it is on — and it is worse than the 25% gap
between the two numbers, because the critical moisture is subtracted from it. Measured on the
worked case in `tests/test_isolation_ops.py` (80 kg dry solid, X_c = 0.10, target 0.005), passing
0.20 where 0.25 was meant reports the constant-rate period **33% low** and the whole cycle 11% low.

**Three things this is not.**

- It is not a **dryer model**. `N_c` is a measured constant from a drying curve on this material in
  this dryer, and it folds in the heat input, the gas velocity, the humidity and the geometry.
  Nothing here derives one, and a `N_c` transferred from a different dryer is a different answer.
- It assumes the equilibrium moisture content is **zero**. Where it is not, the `X` values here are
  *free* moisture, `X - X_e`: a solid drying towards 0.5% bound moisture never reaches zero however
  long it runs, and the logarithm would say infinity.
- It is not a **specification**. Loss-on-drying, residual solvent by GC and a polymorph that
  desolvates as it dries are all analytical questions this arithmetic cannot see, and a drying time
  that satisfies this model can still leave a cake off-spec.

The falling-rate leg is a linear-falling-rate idealisation. A material whose falling rate is set by
internal diffusion rather than by surface wetting dries more slowly than this towards the end, and
the error accumulates exactly where the cycle is longest.
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
    #: Moisture removed over the whole cycle, in kg — the mass the condenser or the vent has to
    #: take, which is a separate sizing question this number is the input to.
    moisture_removed_kg: float
    #: True when the charge starts below the critical moisture content, so there is no
    #: constant-rate period at all and the whole cycle is the slower falling-rate leg.
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

    Args:
        dry_solid_mass_kg: Mass of **bone-dry** solid, in kg. Not the wet cake mass: a 100 kg cake
            at X = 0.25 holds 80 kg of dry solid and 20 kg of moisture.
        drying_area_m2: Area available for evaporation, in m². For a filter-dryer or a tray this is
            the exposed surface, not the vessel's heat-transfer area, and agitation changes it.
        constant_rate_kg_per_m2_s: `N_c`, the constant-period drying rate, in kg of moisture per m²
            per second, measured from a drying curve on this material in this dryer. No default:
            it is the whole answer, and it is not a property of the compound.
        initial_moisture_dry_basis: `X₁`, kg moisture per kg dry solid at the start. **Dry basis.**
        critical_moisture_dry_basis: `X_c`, the moisture content where the rate starts to fall, kg
            per kg dry solid, from the same drying curve.
        final_moisture_dry_basis: `X₂`, the target, kg per kg dry solid. Must be above zero: in
            this model the rate falls linearly to zero at zero moisture, so a bone-dry target takes
            infinite time. Where the equilibrium moisture is not zero, pass free moisture
            (`X - X_e`) throughout.

    Returns:
        The two periods, the total, and the moisture removed.

    Raises:
        UnitOpsInputError: If a mass, area or rate is not positive, if the final moisture is at or
            below zero, or if the target is not below the start.

    A target at or above the critical moisture is **answered**, not refused: the cycle finishes in
    the constant-rate period, which the model describes exactly as `m_s·(X₁ - X₂)/(A·N_c)`, and
    `falling_rate_seconds` is zero. It used to be refused with a docstring saying the cycle sat "in
    the falling-rate leg past the point the model describes", which was backwards — it never
    reaches that leg — while the message itself called the case possible.
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
