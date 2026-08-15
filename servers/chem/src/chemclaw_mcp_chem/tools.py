"""The `chem` MCP tool surface: bench chemistry over RDKit.

**These docstrings are the prompt.** Argument names, defaults and this prose are what the agent
reads before deciding whether to call a tool and what to pass it, and they are carried over from
Chemclaw3's own `chem` connector word for word — several sentences in them exist because a live run
got something wrong in a way that was measured, and shortening one would delete the measurement.

Four capabilities, all pure functions of their arguments plus a read of a vendored table: no store,
no durable state, no network.

**"Cheap" is relative to a DFT job, not to an event loop.** RDKit parsing, `Descriptors.MolWt` and
especially 2D-coordinate generation plus SVG rendering are CPU-bound C++ that holds the GIL for
milliseconds to tens of milliseconds, and one process answers every connected chat turn on one
loop — Chemclaw3 load-tested this connector and measured throughput flat from 10 to 50 concurrent
users, the signature of exactly that. So each tool does its RDKit work in a worker thread
(`asyncio.to_thread`) and the coroutine only awaits it. RDKit releases the GIL for the heavy
passes, so the threads are real parallelism on a multi-CPU pod. This is the opposite conclusion
from `props`, and for a measured reason rather than a stylistic one.
"""

from __future__ import annotations

import asyncio

from mcp.server.fastmcp import FastMCP

from chemclaw_mcp_chem.engine import stoichiometry
from chemclaw_mcp_chem.engine.depiction import render_svg
from chemclaw_mcp_chem.engine.reagents import ResolvedCompound, resolve_compound_name

server = FastMCP("chem")


@server.tool()
async def resolve_compound(name: str) -> ResolvedCompound | None:
    """Resolve a reagent name, abbreviation, or SMILES to its canonical structure.

    Use this whenever the chemist names a reagent in words ("DIPEA", "Pd(dppf)Cl2", "2-MeTHF")
    before calling any tool that needs a SMILES — the property calculators, the similarity search,
    and the substructure search all take structures, not names.

    Returns `None` when the name is not recognised. That is a real answer: say the reagent is not
    in the known set rather than guessing a structure, because a wrong structure would silently
    corrupt every downstream calculation and search.

    Args:
        name: What the chemist wrote — a trivial name, an abbreviation, or a SMILES string.

    Returns:
        The canonical structure with the name it was recognised as, or `None` if unknown.
    """
    # An unrecognised name falls through to an RDKit canonicalisation attempt, so this is not the
    # dictionary lookup it looks like.
    return await asyncio.to_thread(resolve_compound_name, name)


@server.tool()
async def stoichiometry_table(
    basis: str,
    basis_mass_g: float,
    reagents: list[str],
    equivalents: list[float],
    solvents: list[str] | None = None,
    volumes: list[float] | None = None,
) -> stoichiometry.ChargeTable:
    """Build a charge table: what to weigh and measure out for a batch, scaled to the basis.

    Answers the everyday bench question — "for 250 g of the starting material at 1.2 equiv of base
    in 10 volumes of THF, what do I charge?" — deterministically, from molecular weights and
    densities.

    **A charge expressed in volumes goes in `solvents`/`volumes`.** Expressing "THF/water 4:1 at 10
    volumes" as molar equivalents is not a rounding error: done once on a 2 kg basis it put the
    principal solvent out by a factor of 2.17, and the answer then certified the figures as
    self-consistent. Converting volumes yourself is the mistake this argument pair exists to remove.

    **A substance is not owned by one of the two paths, and the tool does not police which you
    use.** Acetic acid at 1.5 equiv, water in a hydrolysis, methanol in an esterification, DMSO as
    the Swern oxidant and DMF as the Vilsmeier reagent are all charged by molar equivalent and all
    have a density on file. Only the chemist knows which reading was meant, so pass the charge in
    the units it was *specified* in and the table reports which those were on each row's `role`.

    Args:
        basis: The limiting reagent (name or SMILES); its mass sets the scale.
        basis_mass_g: How much of the limiting reagent is charged, in grams.
        reagents: The other species charged by molar equivalent (names or SMILES), in order.
        equivalents: Molar equivalents for each entry of `reagents`, same order and length.
        solvents: The species charged by volume (names or SMILES), in order.
        volumes: Process "volumes" for each entry of `solvents` — millilitres per gram of basis,
            same order and length. A 4:1 THF/water mixture at 10 total volumes is `[8.0, 2.0]`.

    Returns:
        One row per species with its molar amount and the mass to weigh out, and for solvents the
        density and the volume to measure. Reagent names that cannot be resolved are listed in
        `unresolved` and carry no row — never a guessed mass. A solvent that cannot be resolved, or
        whose density is not on file, is an error instead: a silently dropped solvent looks like a
        complete table while flattering every mass metric derived from it.
    """
    # One offload for the whole table rather than one per species: a 10-reagent charge table is
    # 11 RDKit parses, and hopping to a worker thread per parse would cost more than it saves.
    return await asyncio.to_thread(
        stoichiometry.charge_table,
        basis,
        basis_mass_g,
        reagents,
        equivalents,
        solvents or [],
        volumes or [],
    )


@server.tool()
async def green_metrics(
    input_masses_g: list[float], product_mass_g: float
) -> stoichiometry.GreenMetrics:
    """Compute the E-factor and PMI of a set of conditions.

    Use this to compare routes or condition sets on waste, not only on yield — "comparable yield at
    half the PMI" is a real process-development goal and the agent had no way to answer it. Pair it
    with `stoichiometry_table`, whose `mass_g` column is exactly this input.

    E-factor is kg waste per kg product (Sheldon); PMI is total input mass per kg product, and the
    two differ by exactly 1 by construction. Lower is better for both.

    Args:
        input_masses_g: Every charged species' mass in grams — reagents, catalyst, and solvent.
            Omitting solvent is the usual way these numbers get flattered; include it. Take the
            `mass_g` of *every* row of the charge table, including the `solvent` ones, which is
            why they share one list there.
        product_mass_g: Isolated product mass in grams. Must be positive.

    Returns:
        Both metrics plus the masses behind them, so the number can be checked rather than trusted.
    """
    # Arithmetic over a list of floats, with no RDKit in it: the one tool here that would gain
    # nothing from a worker thread, so it does not take one.
    return stoichiometry.green_metrics(input_masses_g, product_mass_g)


@server.tool()
async def render_structure(smiles: str) -> str:
    """Draw a molecule or reaction as an SVG the chat surface can show inline.

    Use this when a structure is the answer, or when naming several related structures in prose
    would be ambiguous — a chemist reads a drawing far faster than a SMILES string.

    Args:
        smiles: A molecule SMILES, or a reaction SMILES (`reactants>>products`).

    Returns:
        An inline SVG document.
    """
    return await asyncio.to_thread(render_svg, smiles)
