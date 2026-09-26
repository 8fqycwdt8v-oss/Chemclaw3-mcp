"""Constant-pressure cake filtration: the time to pass a volume of filtrate through a growing cake.

Integrating Darcy's law through a cake whose thickness grows with the filtrate already passed gives
the standard parabolic law:

    t = µ·alpha·c/(2·A²·ΔP) · V²  +  µ·R_m/(A·ΔP) · V

The first term is the **cake**, quadratic in volume because every litre that passes leaves solids
that the next litre has to flow through. The second is the **filter medium**, linear. Which one
dominates decides what to do about a slow filtration: a cake-dominated one is fixed in the
crystalliser, a medium-dominated one by the cloth, so the split is returned rather than just the
total.

**The cake is assumed incompressible**, which is the assumption most likely to be wrong on a real
plant filter and the one that fails in the dangerous direction. A compressible cake — and a
gelatinous, fine, or needle-like organic solid usually is one — has an `alpha` that rises
with pressure, so pushing harder buys less than this predicts and can buy nothing at all.
There is no `alpha = alpha_0·ΔP^s` compressibility exponent here, because fitting one
needs filtration tests at several pressures and a server that invented `s` would be
inventing the answer.

**`alpha` and `R_m` are inputs, from a leaf test or a filtration test on this slurry at this
pressure.** They are not properties of the compound, they are properties of the *crystals* — habit,
size distribution and how the batch was cooled — which is why the same product filters in forty
minutes one week and six hours the next with nothing in the recipe changed. Nothing in this system
holds a specific cake resistance for anything.

**What this is not.** No wash, no displacement efficiency, no cake moisture, no deliquoring, no
blow-down, no centrifuge. A wash volume in particular is *not* derivable here: displacement
efficiency is a measured property of the cake, and a plausible "three displacements" rule quoted as
an answer is exactly the failure Chemclaw3's probe set names for this question.
"""

from __future__ import annotations

from dataclasses import dataclass

from chemclaw_mcp_unitops.engine.validation import (
    UnitOpsInputError,
    finite_result,
    non_negative,
    positive,
)

__all__ = ["CakeFiltration", "filtration_time"]


@dataclass(frozen=True)
class CakeFiltration:
    """How long a filtration takes, and which resistance is responsible."""

    total_time_seconds: float
    total_time_minutes: float
    total_time_hours: float
    #: The cake's share of the time, in seconds — the quadratic term.
    cake_time_seconds: float
    #: The medium's share, in seconds — the linear term.
    medium_time_seconds: float
    #: The cake term as a fraction of the total. Above ~0.9 the cloth is irrelevant and the answer
    #: is in the crystallisation; below ~0.5 the medium is worth looking at first.
    cake_fraction_of_time: float
    #: Mean filtrate flux over the whole filtration, in m³ per m² per hour — the number a filter is
    #: usually sized on and the one that compares across areas.
    average_flux_m3_per_m2_h: float
    #: The instantaneous rate when the last of the filtrate passes, in m³/s. It is the slowest the
    #: filtration ever runs, and the number that says whether the end is worth waiting for.
    final_rate_m3_per_s: float
    #: Dry cake deposited, in kg, from the solids loading and the filtrate volume.
    cake_mass_kg: float


def filtration_time(
    *,
    filtrate_volume_m3: float,
    filter_area_m2: float,
    pressure_drop_pa: float,
    filtrate_viscosity_pa_s: float,
    specific_cake_resistance_m_per_kg: float,
    dry_cake_per_filtrate_kg_per_m3: float,
    medium_resistance_per_m: float = 0.0,
) -> CakeFiltration:
    """Time to filter a given volume at constant pressure, and the cake/medium split.

    Args:
        filtrate_volume_m3: Filtrate to be collected, in m³ (100 L = 0.1 m³).
        filter_area_m2: Filtration area, in m². A 30-inch Nutsche is about 0.46 m² — the *area*,
            not the diameter, and the time goes as its square.
        pressure_drop_pa: Pressure difference across cake and medium, in Pa. 1 bar = 1.0e5 Pa; a
            full vacuum is at most about 1.0e5 Pa of driving force and usually less.
        filtrate_viscosity_pa_s: Viscosity of the **filtrate**, in Pa·s (1 cP = 0.001 Pa·s) — the
            mother liquor that flows, not the slurry.
        specific_cake_resistance_m_per_kg: `alpha`, in m/kg, from a filtration or leaf test on this
            slurry at this pressure. Ordinary organic cakes span 1e9 (free-filtering, coarse) to
            1e13 and beyond (fine or gelatinous), so it is the input that decides the answer and it
            has no default.
        dry_cake_per_filtrate_kg_per_m3: `c`, kg of dry cake deposited per m³ of filtrate collected.
            For a dilute slurry this is close to the solids concentration in the feed.
        medium_resistance_per_m: `R_m`, the cloth's own resistance in 1/m. Defaults to 0, which
            gives the cake-only answer — an honest lower bound rather than an invented cloth, and
            the returned split says how much of the time it accounted for.

    Returns:
        The time, its two contributions, the mean flux and the rate at the end.

    Raises:
        UnitOpsInputError: If a volume, area, pressure, viscosity, resistance or loading is not
            positive, the medium resistance is negative, or a result overflows or underflows
            a floating-point number.
    """
    positive(filtrate_volume_m3, "the filtrate volume")
    positive(filter_area_m2, "the filter area")
    positive(pressure_drop_pa, "the pressure drop")
    positive(filtrate_viscosity_pa_s, "the filtrate viscosity")
    positive(specific_cake_resistance_m_per_kg, "the specific cake resistance")
    positive(dry_cake_per_filtrate_kg_per_m3, "the dry cake per unit filtrate")
    non_negative(medium_resistance_per_m, "the medium resistance")

    cake_time = finite_result(
        lambda: (
            (
                filtrate_viscosity_pa_s
                * specific_cake_resistance_m_per_kg
                * dry_cake_per_filtrate_kg_per_m3
                * filtrate_volume_m3**2
            )
            / (2.0 * filter_area_m2**2 * pressure_drop_pa)
        ),
        "the cake filtration time",
    )
    medium_time = finite_result(
        lambda: (
            (filtrate_viscosity_pa_s * medium_resistance_per_m * filtrate_volume_m3)
            / (filter_area_m2 * pressure_drop_pa)
        ),
        "the medium filtration time",
    )
    total = finite_result(lambda: cake_time + medium_time, "the total filtration time")
    # **Every term is a product of inputs, so it can underflow as well as overflow.** A total of
    # exactly zero is not a fast filtration, it is a number too small for a float — and dividing by
    # it below used to leave as a bare `ZeroDivisionError`, an opaque `error_id` to the caller.
    if total <= 0.0:
        raise UnitOpsInputError(
            "the filtration time underflows to zero for these inputs, which is not a physical "
            "answer; check the units of the viscosity and the specific cake resistance."
        )

    # dV/dt at the end, from Darcy's law with the whole cake in place. Written from the differential
    # form rather than differentiated out of `total`, which is what lets `engine/selftest.py` check
    # one against a finite difference of the other.
    final_rate = finite_result(
        lambda: (
            (filter_area_m2 * pressure_drop_pa)
            / (
                filtrate_viscosity_pa_s
                * (
                    specific_cake_resistance_m_per_kg
                    * dry_cake_per_filtrate_kg_per_m3
                    * filtrate_volume_m3
                    / filter_area_m2
                    + medium_resistance_per_m
                )
            )
        ),
        "the final filtration rate",
    )
    return CakeFiltration(
        total_time_seconds=total,
        total_time_minutes=total / 60.0,
        total_time_hours=total / 3600.0,
        cake_time_seconds=cake_time,
        medium_time_seconds=medium_time,
        cake_fraction_of_time=cake_time / total,
        average_flux_m3_per_m2_h=finite_result(
            lambda: filtrate_volume_m3 / filter_area_m2 / (total / 3600.0), "the average flux"
        ),
        final_rate_m3_per_s=final_rate,
        cake_mass_kg=finite_result(
            lambda: dry_cake_per_filtrate_kg_per_m3 * filtrate_volume_m3, "the cake mass"
        ),
    )
