"""Charge-table and green-metric arithmetic: what to weigh out, and what it costs in waste.

Two calculations that share one input and are therefore written in one module: the charge table
produces a `mass_g` per species, and the green metrics are computed from exactly those masses.
Keeping them apart invited the mistake they are both vulnerable to — leaving the solvent out — and
the row shape below is what stops it.

Nothing here touches a transport. The wire models are pydantic because the values cross a tool
boundary as structured content, which is a serialisation concern rather than a transport one.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from chemclaw_mcp_chem.engine.chem import molecular_weight
from chemclaw_mcp_chem.engine.reagents import density_of, resolve_compound_name

__all__ = ["ChargeRow", "ChargeTable", "GreenMetrics", "charge_table", "green_metrics"]


class ChargeRow(BaseModel):
    """One row of a charge table: what to weigh or measure out for a given species.

    Solvents share the row shape rather than living in a second list, and that is deliberate:
    `green_metrics` takes the `mass_g` column of this table as its input, and a separate list would
    invite a caller to pass the reagent masses alone — which is precisely how E-factor and PMI get
    flattered on the term that dominates them. Every row therefore carries a real mass and real
    moles, however the charge was expressed.
    """

    name: str
    smiles: str
    # Which quantity the chemist actually specified for this species, so a reader can see whether
    # a number was given or derived. Solvent moles and equivalents are always derived.
    role: Literal["basis", "reagent", "solvent"]
    equivalents: float
    molecular_weight: float
    moles_mmol: float
    mass_g: float
    # Populated for solvents only — a reagent charged by mass has no volume to measure out.
    density_g_per_ml: float | None = None
    volume_ml: float | None = None


class ChargeTable(BaseModel):
    """A charge table for one batch: the limiting reagent plus every other species scaled to it."""

    basis_name: str
    basis_mass_g: float
    rows: list[ChargeRow] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)


class GreenMetrics(BaseModel):
    """Green-chemistry mass metrics for one batch, with the masses they were derived from."""

    total_input_kg: float
    product_kg: float
    waste_kg: float
    e_factor: float
    pmi: float


def charge_table(
    basis: str,
    basis_mass_g: float,
    reagents: list[str],
    equivalents: list[float],
    solvents: list[str],
    volumes: list[float],
) -> ChargeTable:
    """Scale every charged species to the limiting reagent's mass.

    Args:
        basis: The limiting reagent, as a name or a SMILES.
        basis_mass_g: How much of it is charged, in grams. Must be positive.
        reagents: The species charged by molar equivalent, in order.
        equivalents: One molar equivalent per entry of `reagents`, same order and length.
        solvents: The species charged by volume, in order.
        volumes: Process "volumes" — millilitres per gram of basis — one per entry of `solvents`.

    Returns:
        One row per species, plus the reagent names that could not be resolved.

    Raises:
        ValueError: the paired lists differ in length, a quantity is not positive, the basis does
            not resolve, or a solvent does not resolve or has no density on file.
    """
    if len(reagents) != len(equivalents):
        raise ValueError(
            f"{len(reagents)} reagents but {len(equivalents)} equivalents; they must match"
        )
    if len(solvents) != len(volumes):
        raise ValueError(f"{len(solvents)} solvents but {len(volumes)} volumes; they must match")
    if basis_mass_g <= 0:
        raise ValueError("basis_mass_g must be positive")
    if any(volume <= 0 for volume in volumes):
        raise ValueError("every entry of volumes must be positive")

    anchor = resolve_compound_name(basis)
    if anchor is None:
        raise ValueError(f"could not resolve the limiting reagent {basis!r}")
    anchor_mw = molecular_weight(anchor.smiles)
    basis_mmol = (basis_mass_g / anchor_mw) * 1000.0
    table = ChargeTable(basis_name=anchor.name, basis_mass_g=basis_mass_g)
    table.rows.append(
        ChargeRow(
            name=anchor.name,
            smiles=anchor.smiles,
            role="basis",
            equivalents=1.0,
            molecular_weight=anchor_mw,
            moles_mmol=basis_mmol,
            mass_g=basis_mass_g,
        )
    )
    for reagent, equiv in zip(reagents, equivalents, strict=True):
        match = resolve_compound_name(reagent)
        if match is None:
            table.unresolved.append(reagent)
            continue
        weight = molecular_weight(match.smiles)
        mmol = basis_mmol * equiv
        table.rows.append(
            ChargeRow(
                name=match.name,
                smiles=match.smiles,
                role="reagent",
                equivalents=equiv,
                molecular_weight=weight,
                moles_mmol=mmol,
                mass_g=mmol * weight / 1000.0,
            )
        )
    for solvent, solvent_volumes in zip(solvents, volumes, strict=True):
        table.rows.append(_solvent_row(solvent, solvent_volumes, basis_mass_g, basis_mmol))
    return table


def _solvent_row(solvent: str, volumes: float, basis_mass_g: float, basis_mmol: float) -> ChargeRow:
    """One solvent charge, converted from volumes to a real mass — or an honest refusal.

    Both refusals are errors rather than an `unresolved` entry, unlike an unrecognised reagent. The
    asymmetry is deliberate: a chemist reads a charge list line by line and sees a missing reagent,
    whereas a missing solvent leaves a table that looks complete and quietly halves the E-factor
    and PMI computed from its masses. Neither a zero nor a guessed 1 g/mL is an acceptable stand-in.
    """
    match = resolve_compound_name(solvent)
    if match is None:
        raise ValueError(f"could not resolve the solvent {solvent!r}")
    density = density_of(solvent)
    if density is None:
        raise ValueError(
            f"no density on file for {match.name!r}, so its volume cannot be converted to a mass — "
            "convert the volume to molar equivalents yourself and pass it in `reagents`, or add "
            "its density to the reagent table"
        )
    volume_ml = volumes * basis_mass_g
    mass_g = volume_ml * density
    weight = molecular_weight(match.smiles)
    mmol = mass_g / weight * 1000.0
    return ChargeRow(
        name=match.name,
        smiles=match.smiles,
        role="solvent",
        equivalents=mmol / basis_mmol,
        molecular_weight=weight,
        moles_mmol=mmol,
        mass_g=mass_g,
        density_g_per_ml=density,
        volume_ml=volume_ml,
    )


def green_metrics(input_masses_g: list[float], product_mass_g: float) -> GreenMetrics:
    """E-factor and PMI from the charged masses and the isolated product mass.

    E-factor is kg waste per kg product (Sheldon); PMI is total input mass per kg product, and the
    two differ by exactly 1 by construction.

    Raises:
        ValueError: the product mass is not positive, an input mass is negative, or the total input
            is below the product mass — mass cannot appear from nowhere, and silently reporting the
            resulting negative E-factor would read as an implausibly green process.
    """
    if product_mass_g <= 0:
        raise ValueError("product_mass_g must be positive")
    if any(mass < 0 for mass in input_masses_g):
        raise ValueError("input masses must not be negative")
    total = sum(input_masses_g)
    if total < product_mass_g:
        raise ValueError(
            f"total input {total:g} g is below the product mass {product_mass_g:g} g — "
            "the mass balance is unsound (is a reagent or the solvent missing?)"
        )
    waste = total - product_mass_g
    return GreenMetrics(
        total_input_kg=total / 1000.0,
        product_kg=product_mass_g / 1000.0,
        waste_kg=waste / 1000.0,
        e_factor=waste / product_mass_g,
        pmi=total / product_mass_g,
    )
