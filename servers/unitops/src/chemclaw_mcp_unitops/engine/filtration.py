"""Constant-pressure cake filtration: the time to pass a volume of filtrate through a growing cake.

Darcy's law through a growing cake gives the parabolic law:

    t = µ·alpha·c/(2·A²·ΔP) · V²  +  µ·R_m/(A·ΔP) · V

The quadratic term is the cake (fixed in the crystalliser), the linear one the medium (fixed by the
cloth), so the split is returned. The cake is assumed **incompressible**, which fails in the
dangerous direction: compressible organic cakes gain less from more pressure than predicted. No
compressibility exponent is invented. `alpha` and `R_m` are measured inputs from a filtration test
on this slurry, properties of the crystals rather than the compound. No wash, cake moisture,
deliquoring or centrifuge; wash volumes are not derivable here.
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
    #: Cake term as a fraction of the total: above ~0.9 look at the crystallisation; below ~0.5 the
    #: medium first.
    cake_fraction_of_time: float
    #: Mean filtrate flux over the filtration, m³/(m²·h); what a filter is usually sized on.
    average_flux_m3_per_m2_h: float
    #: Instantaneous rate as the last filtrate passes, m³/s: the slowest the filtration runs.
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
        filtrate_volume_m3: Filtrate to be collected, m³ (100 L = 0.1 m³).
        filter_area_m2: Filtration area, m² (a 30-inch Nutsche is about 0.46 m²); time goes as its
            square.
        pressure_drop_pa: Pressure difference across cake and medium, Pa (1 bar = 1.0e5 Pa; vacuum
            gives at most about that).
        filtrate_viscosity_pa_s: Viscosity of the **filtrate**, Pa·s (1 cP = 0.001 Pa·s), not the
            slurry.
        specific_cake_resistance_m_per_kg: `alpha`, m/kg, from a test on this slurry at this
            pressure; organic cakes span about 1e9 to 1e13+. No default.
        dry_cake_per_filtrate_kg_per_m3: `c`, kg dry cake per m³ filtrate; near the feed solids
            concentration for a dilute slurry.
        medium_resistance_per_m: `R_m`, the cloth's resistance, 1/m. Defaults to 0, a cake-only
            lower bound.

    Returns:
        The time, its two contributions, the mean flux and the rate at the end.

    Raises:
        UnitOpsInputError: A volume, area, pressure, viscosity, resistance or loading is not
            positive, the medium resistance is negative, or a result overflows or underflows.
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
    # Products of inputs can underflow: a zero total is too small for a float, not a fast
    # filtration.
    if total <= 0.0:
        raise UnitOpsInputError(
            "the filtration time underflows to zero for these inputs, which is not a physical "
            "answer; check the units of the viscosity and the specific cake resistance."
        )

    # dV/dt from Darcy's law with the whole cake, written from the differential form so
    # `engine/selftest.py` can check it against a finite difference of `total`.
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
