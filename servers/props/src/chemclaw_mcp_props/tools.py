"""The `props` MCP tool surface: solvent properties, vapour pressure, and swap shortlists.

**These docstrings are the prompt.** Argument names, defaults and this prose are what the agent
reads before deciding whether to call a tool and what to pass it — so each one says what the tool is
for, what units it speaks, and what it is *not* evidence of. A tool whose docstring omits the last
of those gets used outside its range, and the resulting number reaches a chemist with no warning
attached.

Every answer carries `source`: this server's numbers come from one vendored, checksummed table and
nowhere else, and a property without its provenance is not something anybody can put in a report.

Everything here is a dictionary lookup and a few floating-point operations — microseconds — so the
tools are synchronous. That is a measured statement about *this* server rather than a house style:
Chemclaw3's `chem` connector pushes its RDKit work to `asyncio.to_thread` because 2D-coordinate
generation holds the GIL for tens of milliseconds and flattened its throughput under load. Nothing
here does that; a server that starts doing real work must revisit this.
"""

from __future__ import annotations

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from chemclaw_mcp_props.engine import correlations, records, selection

server = FastMCP("props")

# How many solvents one `compare_solvent_properties` call may name.
#
# **Derived from the corpus rather than chosen**, because the table is what makes the number
# principled: a comparison naming more solvents than exist cannot be a comparison, only duplicates
# or unknowns, so the largest legitimate request is "every solvent you have". A literal here would
# be a magic number that either forbids a real question or leaves the cost unbounded, and it would
# go stale the first time a row is added.
#
# It exists because the unbounded version was measured, not imagined: 100 000 x "dcm" was a 700 KB
# request — 70% of `DEFAULT_MAX_REQUEST_BYTES`, so accepted — that returned 81 601 345 B after
# 14.83 s, during which a `/healthz` probe waited 14.47 s. The body is synchronous, so that was the
# event loop held in one block, and this module's own opening paragraph ("a dictionary lookup and a
# few floating-point operations — microseconds — so the tools are synchronous") was false for as
# long as the input could be any length. Bounding the input is what makes it true again.
MAX_COMPARED_SOLVENTS = len(records.all_solvents())


class SolventSummary(BaseModel):
    """One line per solvent, for choosing what to look at in detail."""

    name: str
    aliases: list[str]
    cas: str
    bp_c: float
    greenness_band: str
    ich_class: str


class SolventRecord(BaseModel):
    """Everything the vendored table holds for one solvent."""

    name: str
    aliases: list[str]
    cas: str
    smiles: str
    formula: str
    molecular_weight: float
    boiling_point_c: float
    melting_point_c: float
    density_20c_g_per_ml: float
    flash_point_c: float | None = Field(
        description="Closed-cup flash point. `null` means the solvent has none — not that it is 0."
    )
    dielectric_constant: float
    hansen_dispersion: float
    hansen_polar: float
    hansen_hydrogen_bonding: float
    water_miscibility: str
    peroxide_former: bool
    ich_class: str
    ich_limit_ppm: float | None
    greenness_band: str
    hazard_flags: list[str]
    has_antoine_constants: bool
    source: str


class VapourPressureResult(BaseModel):
    """A vapour pressure and the route that produced it. Quote `method` and `caveat` together."""

    solvent: str
    temperature_c: float
    pressure_mbar: float
    pressure_bar: float
    pressure_mmhg: float
    method: str
    caveat: str
    source: str


class BoilingPointResult(BaseModel):
    """The temperature a solvent boils at under a stated vacuum."""

    solvent: str
    pressure_mbar: float
    boiling_point_c: float
    normal_boiling_point_c: float
    method: str
    caveat: str
    source: str


class SwapCandidateOut(BaseModel):
    """One candidate replacement, with the constraints it failed already named."""

    name: str
    hansen_distance: float
    similarity: str
    boiling_point_c: float
    boiling_point_delta_c: float
    greenness_band: str
    greenness_change: str
    ich_class: str
    ich_limit_ppm: float | None
    flash_point_c: float | None
    water_miscibility: str
    peroxide_former: bool
    hazard_flags: list[str]
    blockers: list[str]
    passes_constraints: bool


class SwapResult(BaseModel):
    """A shortlist for one solvent, plus the sentence that must travel with it."""

    replacing: str
    candidates: list[SwapCandidateOut]
    basis: str
    source: str


class ComparisonRow(BaseModel):
    """One solvent's row in a side-by-side comparison."""

    name: str
    boiling_point_c: float
    melting_point_c: float
    flash_point_c: float | None
    density_20c_g_per_ml: float
    dielectric_constant: float
    water_miscibility: str
    peroxide_former: bool
    ich_class: str
    ich_limit_ppm: float | None
    greenness_band: str
    hazard_flags: list[str]


class ComparisonResult(BaseModel):
    """A comparison table and the names it could not resolve."""

    rows: list[ComparisonRow]
    unknown: list[str]
    source: str


def _record(solvent: records.Solvent) -> SolventRecord:
    """Project a table row onto the wire model, keeping every absent value absent."""
    return SolventRecord(
        name=solvent.name,
        aliases=list(solvent.aliases),
        cas=solvent.cas,
        smiles=solvent.smiles,
        formula=solvent.formula,
        molecular_weight=solvent.mw,
        boiling_point_c=solvent.bp_c,
        melting_point_c=solvent.mp_c,
        density_20c_g_per_ml=solvent.density_20c,
        flash_point_c=solvent.flash_point_c,
        dielectric_constant=solvent.dielectric,
        hansen_dispersion=solvent.hansen_d,
        hansen_polar=solvent.hansen_p,
        hansen_hydrogen_bonding=solvent.hansen_h,
        water_miscibility=solvent.water_miscibility,
        peroxide_former=solvent.peroxide_former,
        ich_class=solvent.ich_class,
        ich_limit_ppm=solvent.ich_limit_ppm,
        greenness_band=solvent.greenness_band,
        hazard_flags=list(solvent.hazard_flags),
        has_antoine_constants=solvent.antoine is not None,
        source=solvent.provenance,
    )


@server.tool()
def list_solvents() -> list[SolventSummary]:
    """List every solvent this server knows, with boiling point and hazard band.

    Call this first when a name might not be in the table, or when the chemist has asked an
    open question ("what could we use instead of DMF?") and you need the candidate set before
    narrowing it. The table is fixed and vendored — this server cannot look up a solvent that is
    not listed here, and will say so rather than guessing.

    Returns:
        One summary per solvent: name, the aliases it also answers to, CAS, normal boiling point,
        greenness band and ICH Q3C class.
    """
    return [
        SolventSummary(
            name=solvent.name,
            aliases=list(solvent.aliases),
            cas=solvent.cas,
            bp_c=solvent.bp_c,
            greenness_band=solvent.greenness_band,
            ich_class=solvent.ich_class,
        )
        for solvent in records.all_solvents()
    ]


@server.tool()
def solvent_properties(name: str) -> SolventRecord:
    """Every recorded property of one solvent: physical, safety, and regulatory.

    The tool to reach for whenever a number about a solvent would otherwise be recalled from
    memory — boiling and melting point, density, flash point, dielectric constant, Hansen
    parameters, water miscibility, peroxide-forming tendency, ICH Q3C class and its residual-solvent
    limit, and the hazard flags behind the greenness band.

    Two fields are routinely misread, so read them carefully. `flash_point_c` is `null` for
    solvents that genuinely have none (dichloromethane, chloroform, water) — that is "no flash
    point", not "flash point at 0 °C". `ich_class` is `not_listed` for solvents ICH Q3C does not
    name (CPME, dimethyl carbonate, propylene carbonate); that means the guideline is silent, not
    that the solvent is unrestricted, and a limit still has to be justified.

    Args:
        name: Any spelling a chemist would write — trivial name, abbreviation, or CAS number.
            `2-MeTHF`, `MeTHF`, `2-methyltetrahydrofuran` and `96-47-9` all resolve to one row.

    Returns:
        The full record, with `source` naming the vendored dataset and its licence.

    Raises:
        ValueError: if the solvent is not in the table. Report that plainly — do not substitute a
            similar solvent, because every downstream number would then be about a different liquid.
    """
    return _record(records.require(name))


@server.tool()
def vapour_pressure(name: str, temperature_c: float) -> VapourPressureResult:
    """Vapour pressure of a solvent at a temperature — the stripping and drying question.

    Use it for "will this come off at 40 °C under 50 mbar", for judging how much solvent a nitrogen
    sweep will carry, and for the vapour side of a flammability assessment.

    **The number arrives with a `method`, and the two methods are not equally good.** `antoine` is a
    fit from the table, worth about a percent, and is only ever returned inside the temperature
    range that fit was made over — the `caveat` names that range, and outside it this server falls
    back rather than extrapolating a correlation past where it is known to diverge.
    `clausius_clapeyron` extrapolates from the normal boiling point and is exact only there.
    `clausius_clapeyron_trouton` additionally
    *estimates* the enthalpy of vaporisation, and underestimates it for alcohols, acids and water —
    so it reads high below the boiling point. Quote `method` and `caveat` whenever you report the
    value; a chemist sizing a condenser needs to know which of the three they were given.

    Args:
        name: The solvent, in any spelling the table answers to.
        temperature_c: Temperature in degrees Celsius.

    Returns:
        The pressure in mbar, bar and mmHg, plus `method`, `caveat` and `source`.

    Raises:
        ValueError: if the solvent is unknown, or the temperature is below its melting point —
            this server carries liquid vapour pressures only.
    """
    solvent = records.require(name)
    result = correlations.vapour_pressure(solvent, temperature_c)
    return VapourPressureResult(
        solvent=solvent.name,
        temperature_c=result.temperature_c,
        pressure_mbar=round(result.pressure_mbar, 3),
        pressure_bar=round(result.pressure_bar, 6),
        pressure_mmhg=round(result.pressure_mmhg, 3),
        method=result.method,
        caveat=result.caveat,
        source=solvent.provenance,
    )


@server.tool()
def boiling_point_at_pressure(name: str, pressure_mbar: float) -> BoilingPointResult:
    """The temperature a solvent boils at under a given vacuum — for distillation and rotovap work.

    The inverse of `vapour_pressure`, and the more useful direction when the question is "what
    jacket temperature do I need to take this off at 100 mbar" or "can I concentrate this below
    40 °C to protect the product".

    It answers for the *pure* solvent. A real distillation of a mixture boils higher than this and
    changes composition as it goes, and a dissolved solute raises the boiling point further, so
    treat the answer as the floor rather than the set point. The same `method`/`caveat` distinction
    as `vapour_pressure` applies, for the same reason.

    Args:
        name: The solvent, in any spelling the table answers to.
        pressure_mbar: Absolute pressure in the still, in mbar. Atmospheric is about 1013 mbar.

    Returns:
        The boiling temperature at that pressure, alongside the normal boiling point for comparison.

    Raises:
        ValueError: if the solvent is unknown, the pressure is not positive, the solvent would
            freeze before boiling at that vacuum, or the pressure is one it never reaches in the
            range this server models — a pressurised question above the correlation's ceiling gets
            a refusal naming the ceiling, never the ceiling itself dressed up as a boiling point.
    """
    solvent = records.require(name)
    temperature = correlations.boiling_point_at(solvent, pressure_mbar)
    probe = correlations.vapour_pressure(solvent, temperature)
    return BoilingPointResult(
        solvent=solvent.name,
        pressure_mbar=pressure_mbar,
        boiling_point_c=round(temperature, 1),
        normal_boiling_point_c=solvent.bp_c,
        method=probe.method,
        caveat=probe.caveat,
        source=solvent.provenance,
    )


@server.tool()
def solvent_swap_candidates(
    name: str,
    top_n: int = 5,
    allow_worse_greenness: bool = False,
    min_bp_c: float | None = None,
    max_bp_c: float | None = None,
    exclude_peroxide_formers: bool = False,
    require_water_miscibility: str | None = None,
    max_ich_class: str | None = None,
) -> SwapResult:
    """Shortlist replacements for a solvent, nearest in Hansen space first.

    The tool for "we cannot take dichloromethane into the plant — what else?". Candidates are
    filtered on the constraints you state and then ranked by Hansen distance; candidates that fail a
    constraint are still returned, after the ones that pass, each with the constraint it failed
    named in `blockers`. That is deliberate — "toluene is the closest match but it is reprotoxic
    cat 2" is usually the most useful sentence in the answer.

    **A shortlist is not a recommendation.** Hansen distance is a solubility argument only. It has
    no opinion on whether the replacement is inert to the chemistry, dissolves the base, is stable
    at the reaction temperature, or lets the product crystallise — and it will happily rank a
    solvent that reacts with your substrate as "close". Present the list as candidates for the
    chemist to judge, name the constraints that were applied, and say what was not considered.

    Args:
        name: The solvent being replaced.
        top_n: How many candidates to return (default 5).
        allow_worse_greenness: Set true only if the chemist has explicitly accepted a worse hazard
            band. Left false, a candidate in a worse band is returned marked blocked.
        min_bp_c: Reject candidates boiling below this — use it when the process needs reflux at a
            given temperature.
        max_bp_c: Reject candidates boiling above this — use it when the solvent has to be removed
            by distillation under mild conditions.
        exclude_peroxide_formers: Reject ethers and other peroxide formers outright. Worth setting
            for anything that will be stored, concentrated, or taken to dryness.
        require_water_miscibility: `miscible`, `partial` or `immiscible` — the aqueous-workup
            constraint. `immiscible` is what an extraction needs; `miscible` is what a homogeneous
            quench needs.
        max_ich_class: `1`, `2` or `3`. Rejects anything in a worse ICH Q3C class. Solvents the
            guideline does not list are never rejected by this filter — their `ich_class` comes back
            as `not_listed`, and that still needs a justified limit.

    Returns:
        The ranked shortlist, each entry carrying its Hansen distance, boiling-point shift,
        greenness change, ICH class and limit, hazard flags, and any blockers.

    Raises:
        ValueError: if the solvent being replaced is not in the table.
    """
    solvent = records.require(name)
    candidates = selection.swap_candidates(
        solvent,
        top_n=top_n,
        allow_worse_greenness=allow_worse_greenness,
        min_bp_c=min_bp_c,
        max_bp_c=max_bp_c,
        exclude_peroxide_formers=exclude_peroxide_formers,
        require_water_miscibility=require_water_miscibility,
        max_ich_class=max_ich_class,
    )
    return SwapResult(
        replacing=solvent.name,
        candidates=[
            SwapCandidateOut(
                name=candidate.name,
                hansen_distance=candidate.hansen_distance,
                similarity=candidate.similarity,
                boiling_point_c=candidate.bp_c,
                boiling_point_delta_c=candidate.bp_delta_c,
                greenness_band=candidate.greenness_band,
                greenness_change=candidate.greenness_change,
                ich_class=candidate.ich_class,
                ich_limit_ppm=candidate.ich_limit_ppm,
                flash_point_c=candidate.flash_point_c,
                water_miscibility=candidate.water_miscibility,
                peroxide_former=candidate.peroxide_former,
                hazard_flags=list(candidate.hazard_flags),
                blockers=list(candidate.blockers),
                passes_constraints=candidate.passes,
            )
            for candidate in candidates
        ],
        basis=(
            "Ranked by Hansen distance Ra = sqrt(4*dD^2 + dP^2 + dH^2); Ra <= 4 is close, >= 8 is "
            "distant. Solubility similarity only — reactivity, stability, crystallisation "
            "behaviour and cost are not considered."
        ),
        source=solvent.provenance,
    )


@server.tool()
def compare_solvent_properties(
    names: Annotated[list[str], Field(min_length=1, max_length=MAX_COMPARED_SOLVENTS)],
) -> ComparisonResult:
    """Put several solvents side by side on the properties a process decision turns on.

    Use it when the chemist is weighing a named set — "compare 2-MeTHF, THF and MTBE" — rather than
    asking for a shortlist. Unknown names are returned in `unknown` instead of failing the whole
    call, so one typo does not cost the comparison.

    **This compares tabulated physical properties and nothing else.** It reads measured numbers out
    of a vendored table; it runs no calculation, knows nothing about your reaction, and has no
    opinion on which solvent the chemistry prefers. For "which solvent makes this reaction go", the
    question is a computed one and belongs to Chemclaw3's `compare_solvents` job.

    Args:
        names: The solvents to compare, in any spelling the table answers to. At most
            `MAX_COMPARED_SOLVENTS` (the size of the table), since naming more than exist can only
            repeat or misspell them.

    Returns:
        One row per resolved solvent (boiling and melting point, flash point, density, dielectric
        constant, water miscibility, peroxide formation, ICH class and limit, greenness band and
        hazard flags), plus the names that did not resolve.
    """
    rows: list[ComparisonRow] = []
    unknown: list[str] = []
    for name in names:
        solvent = records.find(name)
        if solvent is None:
            unknown.append(name)
            continue
        rows.append(
            ComparisonRow(
                name=solvent.name,
                boiling_point_c=solvent.bp_c,
                melting_point_c=solvent.mp_c,
                flash_point_c=solvent.flash_point_c,
                density_20c_g_per_ml=solvent.density_20c,
                dielectric_constant=solvent.dielectric,
                water_miscibility=solvent.water_miscibility,
                peroxide_former=solvent.peroxide_former,
                ich_class=solvent.ich_class,
                ich_limit_ppm=solvent.ich_limit_ppm,
                greenness_band=solvent.greenness_band,
                hazard_flags=list(solvent.hazard_flags),
            )
        )
    return ComparisonResult(rows=rows, unknown=unknown, source=records.dataset().citation())
