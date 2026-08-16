"""The `calc` MCP tool surface: nine request/response calculators over GFN2-xTB and RDKit.

**These docstrings are the prompt.** Argument names, defaults and this prose are what the agent
reads before deciding whether to call a tool and what to pass it, and they are carried over from
Chemclaw3's own `calc` connector word for word — several sentences exist because a live run got
something wrong in a way that was measured, and shortening one would delete the measurement.

Two edits were made to them and both are the same edit: **every sentence claiming a result is
cached is gone.** "Cached, so repeats are free" was true of a connector sitting on a Postgres
calculation store; it is false of this server, which computes on request and stores nothing. Leaving
it would tell the model a second call is free when it costs another full SCF.

**What replaces the cache is `calc_version` and `calc_key` on every result.** The store did not move
here, so the addressing has to travel: `calc_key` is the flat
`calc_type@calc_version:input_hash:params_hash` string the result *would* be stored under, and
`calc_version` alone is the primary key of Chemclaw3's calibration ledger. Both are derived in this
process because nothing else can derive them — the `tblite`/`rdkit` distributions and any `xtb`
binary live here, and `xtb_cli.binary_version()` answers `"absent"` rather than raising, so a client
deriving the string locally would produce a well-formed value matching zero ledger rows and read as
`UNCALIBRATED` rather than as an error.

**Nothing here is cheap by event-loop standards.** A single point is tens to hundreds of
milliseconds; a Hessian on a drug-sized molecule is minutes. One uvicorn process serves every
connected turn on one loop, so every tool body runs its work in a worker thread
(`asyncio.to_thread`) and the coroutine only awaits it. `tests/test_event_loop_offload.py` asserts
the hop for every one of the nine, because a hop with no test is a property nobody would notice
losing.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess

from mcp.server.fastmcp import FastMCP

from chemclaw_mcp_calc.engine import xtb_props
from chemclaw_mcp_calc.engine.config import settings
from chemclaw_mcp_calc.engine.descriptors import (
    DescriptorInput,
    DescriptorProfile,
    compute_descriptor_profile,
)
from chemclaw_mcp_calc.engine.logd import LogdInput, LogdResult
from chemclaw_mcp_calc.engine.logd import predict_logd as _predict_logd
from chemclaw_mcp_calc.engine.pka import PkaInput, PkaResult
from chemclaw_mcp_calc.engine.pka import calc_version as pka_calc_version
from chemclaw_mcp_calc.engine.pka import predict_pka as _predict_pka
from chemclaw_mcp_calc.engine.solubility import (
    SolubilityInput,
    SolubilityResult,
)
from chemclaw_mcp_calc.engine.solubility import (
    predict_solubility as _predict_solubility,
)
from chemclaw_mcp_calc.engine.structure import structure_from_smiles
from chemclaw_mcp_calc.engine.xtb import XtbInput, XtbResult, run_xtb
from chemclaw_mcp_calc.engine.xtb_opt import OptimizationSummary, OptSpec, optimize_structure
from chemclaw_mcp_calc.engine.xtb_props import (
    ElectronicProperties,
    FukuiMode,
    SiteReactivityResult,
)
from chemclaw_mcp_calc.engine.xtb_spec import XtbSpec
from chemclaw_mcp_calc.engine.xtb_thermo import ThermochemistryResult, ThermoSpec, relax_to_minimum

server = FastMCP("calc")

logger = logging.getLogger(__name__)

__all__ = [
    "compute_electronic_properties",
    "compute_thermochemistry",
    "compute_xtb_energy",
    "optimize_geometry",
    "predict_developability_profile",
    "predict_logd",
    "predict_pka",
    "predict_site_reactivity",
    "predict_solubility",
    "resolve_calculator_versions",
    "server",
]


async def resolve_calculator_versions() -> None:
    """Resolve the xTB backend once at startup, off the event loop, before a request needs it.

    `pka_calc_version()` names the optimizer that relaxes the base, so it resolves the backend — and
    wherever that resolves to the `xtb` binary (always under `CHEMCLAW_XTB_ENGINE=xtb`, and under
    the `auto` default on any image that has the binary), it shells out to `xtb --version` on the
    first call in a process, `lru_cache`d thereafter. Every tool derives a version string, so *any*
    first call in a fresh pod could otherwise hold this process's single event loop — every
    session's stream, not just its own — for up to the 30 s subprocess timeout.

    Guarding each caller would leave the same trap set for the next one, so the resolution is
    hoisted to the one place a process starts. Honest limit: `on_start` is *started*, not awaited
    (see `connector_app`), so a request arriving in the first milliseconds can still win the race
    and pay the resolution once — the window is startup-sized, not per-request.

    Swallows its own failures, as the `on_start` contract requires: a server that refuses to start
    because it could not ask a binary for its version is strictly worse than one that starts and
    resolves the version on first use.
    """
    try:
        version = await asyncio.to_thread(pka_calc_version)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("could not resolve the xTB backend at startup: %s", exc)
        return
    logger.info("calc server resolved its calculator version: %s", version)


@server.tool()
async def compute_xtb_energy(smiles: str, charge: int = 0) -> XtbResult:
    """Compute the GFN2-xTB total energy of a molecule (fast, semiempirical).

    Runs a quick semiempirical single point (no HPC).

    A single-point energy is only meaningful **relatively** — compare it with another energy
    computed the same way (an isomer, a conformer, the same molecule in another solvent). The
    absolute number is not a heat of formation and not comparable with a literature value.

    Args:
        smiles: The molecule as a SMILES string.
        charge: Net molecular charge (0 = neutral).

    Returns:
        The method, charge, and total energy in Hartree, plus `calc_version` and `calc_key` — the
        versioned identity this calculation would be stored under.
    """
    return await asyncio.to_thread(run_xtb, XtbInput(smiles=smiles, charge=charge))


@server.tool()
async def predict_solubility(smiles: str) -> SolubilityResult:
    """Predict aqueous solubility (log S, mol/L) of a molecule, with uncertainty.

    Uses a fast property model (ESOL, Delaney 2004); the result reports an uncertainty that you
    should pass on to the user rather than treating the value as exact.

    Read `estimate.in_domain` before quoting the number. ESOL is a linear equation over neutral,
    single-component, organic molecules — for a salt, a charged species or an organometallic it
    returns a value that is not merely less accurate but undefined, and `estimate.domain_reasons`
    says which of the three applies.

    Args:
        smiles: The molecule as a SMILES string.

    Returns:
        The predicted log solubility, its uncertainty, the model used, the applicability-domain
        verdict, and `calc_version` — which is the key Chemclaw3's calibration ledger scores
        `report_measurement` observations against, so pass it through rather than deriving one.
    """
    return await asyncio.to_thread(_predict_solubility, SolubilityInput(smiles=smiles))


@server.tool()
async def predict_pka(smiles: str) -> PkaResult:
    """Predict a molecule's pKa via GFN2-xTB — an acid site, or a base's conjugate acid.

    Two domains with different accuracy, and `site` on the result says which one ran.
    **Acids** (`site="acid"`): the most acidic O-H/S-H proton — carboxylic acids, phenols,
    alcohols, thiols — reported with ~1.6 units of uncertainty. **Bases** (`site="base"`),
    when there is no acidic proton: the pKa of the *conjugate acid* (pKaH), the number
    tabulated for amines, reported with +/-1.0. An acid site wins when a molecule has both.

    Base coverage is **aromatic and aryl nitrogen only** — pyridines, imidazoles, azoles,
    anilines. Aliphatic amines raise instead of returning a value, and that refusal is
    load-bearing rather than cautious: over 13 reference amines the method ranks them at
    Spearman -0.17, because a continuum solvent cannot represent the ammonium ion's hydrogen
    bonding to water. Report that the value is not predictable rather than substituting
    another tool's output.

    Args:
        smiles: The molecule as a SMILES string.

    Returns:
        The predicted pKa, which site it describes, the protonation/deprotonation energy, the
        uncertainty, and `calc_version` — the exact string Chemclaw3's calibration ledger keys `pka`
        predictions on. It is derived from the installed tblite/RDKit builds, seven calibration
        settings and (where present) the `xtb` binary's version, none of which exist outside this
        server, so record it rather than reconstructing it.
    """
    return await asyncio.to_thread(_predict_pka, PkaInput(smiles=smiles))


@server.tool()
async def compute_electronic_properties(
    smiles: str, solvent: str | None = None
) -> ElectronicProperties:
    """Compute frontier orbitals, dipole, partial charges and bond orders (GFN2-xTB).

    One fast semiempirical calculation gives the HOMO and LUMO energies and their gap
    (eV), the dipole moment (Debye), Mulliken partial charges per atom, and Wiberg
    bond orders per bonded pair. Use it to compare the electronic character of related
    molecules — a smaller gap means a more easily excited/reactive π system, a larger
    dipole a more polar molecule, and the partial charges show where the electron
    density sits. These are semiempirical values on a force-field geometry: compare
    them across similar structures rather than quoting one as an absolute measurement.

    Args:
        smiles: The molecule as a SMILES string.
        solvent: Optional implicit solvent name (e.g. "water", "toluene") for an ALPB
            solvated calculation; omit for gas phase. An unparameterised name is refused with the
            supported list rather than approximated.

    Returns:
        The total energy, HOMO/LUMO/gap in eV, dipole in Debye, per-atom charges and
        the bond orders. Atom indices match the heavy atoms of the canonical SMILES,
        with hydrogens following them.
    """

    def _run() -> ElectronicProperties:
        structure = xtb_props.property_structure(smiles)
        return xtb_props.compute_properties(XtbSpec(task="properties", solvent=solvent), structure)

    return await asyncio.to_thread(_run)


@server.tool()
async def predict_site_reactivity(
    smiles: str, mode: FukuiMode = "electrophilic", top_n: int = 0
) -> SiteReactivityResult:
    """Rank the atoms of a molecule by how susceptible they are to attack (GFN2-xTB).

    Answers regioselectivity questions — which position of a ring is substituted,
    which site is oxidized, where a nucleophile adds — using condensed Fukui indices
    from three fast semiempirical calculations. Choose `mode` by what attacks the
    molecule: "electrophilic" for attack by an electrophile (e.g. aromatic
    nitration/halogenation), "nucleophilic" for attack by a nucleophile (e.g. addition
    to a carbonyl), "radical" for radical chemistry.

    Read the ranking as a hypothesis, not a prediction of yield: it ranks sites
    *within* this molecule only (never between molecules), it describes electronic
    susceptibility alone — sterics, the specific reagent and the solvent are not in
    the model — and a heteroatom often tops the list because of its lone pair, so for
    a ring-substitution question compare the ring carbons with each other.

    The three single points do not depend on `mode` — it only chooses the sort — so every result
    carries all three indices per atom. Read the other rankings off `f_minus`/`f_plus`/`f_zero`
    rather than calling again for a second mode, which would recompute all three SCFs.

    Args:
        smiles: The molecule as a SMILES string. Must be closed-shell (no radicals).
        mode: Which attack to rank for.
        top_n: How many atoms to return, most susceptible first. 0 uses the configured
            default; pass a larger number to see the whole molecule.

    Returns:
        The ranked sites with all three Fukui indices per atom, and the total number
        of atoms the ranking was drawn from. Atom indices match the heavy atoms of the
        canonical SMILES, with hydrogens following them.
    """

    def _run() -> SiteReactivityResult:
        structure = xtb_props.property_structure(smiles)
        result = xtb_props.compute_fukui(XtbSpec(task="fukui"), structure, mode)
        limit = top_n if top_n > 0 else settings.xtb_fukui_top_n
        return result.model_copy(update={"sites": result.sites[:limit]})

    return await asyncio.to_thread(_run)


@server.tool()
async def optimize_geometry(smiles: str, solvent: str | None = None) -> OptimizationSummary:
    """Relax a molecule to its nearest stable 3D shape with GFN2-xTB.

    Every other fast calculation here describes whichever conformer was embedded from
    the SMILES and cleaned up with a force field. This one finds an actual minimum of
    the quantum-mechanical surface, which is what the energy and the frequencies are
    computed on. Use it before comparing energies that need to be trustworthy, and to
    see how far a starting guess was from a real structure — a large `relaxation_kcal`
    on a molecule means the unrelaxed numbers for it were describing a strained shape.

    It finds the *nearest* minimum, not the best one: a flexible molecule has many
    conformers and this relaxes into whichever basin it started in.

    Args:
        smiles: The molecule as a SMILES string.
        solvent: Optional implicit solvent name (e.g. "water", "thf"); omit for gas phase.

    Returns:
        The converged energy, how much the relaxation lowered it, how far the atoms
        moved, and the id of the resulting geometry. The coordinates themselves are not
        returned — a model cannot read 3N Cartesians — but `structure_id` names the geometry and
        `calc_key` addresses the calculation that produced it.
    """

    def _run() -> OptimizationSummary:
        structure = structure_from_smiles(smiles, multiplicity=None, optimize=True)
        return OptimizationSummary.of(optimize_structure(OptSpec(solvent=solvent), structure))

    return await asyncio.to_thread(_run)


@server.tool()
async def compute_thermochemistry(
    smiles: str,
    solvent: str | None = None,
    symmetry_number: int = 1,
    temperature_k: float = 0.0,
    top_bands: int = 0,
) -> ThermochemistryResult:
    """Compute vibrational frequencies, an IR spectrum, and free energy (GFN2-xTB).

    Optimizes the molecule, then takes its second derivatives. That gives three things:
    whether the structure is a genuine minimum (`is_minimum`, with any imaginary
    frequencies listed), a predicted IR spectrum with band positions and intensities,
    and ideal-gas thermochemistry — zero-point energy, enthalpy, entropy and Gibbs free
    energy. Use the spectrum to test a proposed structure against a measured one, and
    the free energy for equilibrium questions that an electronic energy cannot answer.

    Read it with three limits in mind. Frequencies are semiempirical and systematically
    a few percent off, so compare *patterns and orderings* with a measured spectrum
    rather than expecting positions to match. Everything describes one conformer, not
    the molecule's real population. And the entropy depends on the rotational symmetry
    number, which defaults to 1 — pass the true value (2 for water, 3 for ammonia, 6
    for ethane, 12 for benzene) when the molecule is symmetric, or the entropy comes
    out too high by R·ln(symmetry number).

    **This is the most expensive tool here**: the second derivatives cost 6N single points, so a
    drug-sized molecule is minutes rather than seconds and a molecule above the configured atom
    limit is refused outright. Ask for it when the free energy or the spectrum is the question, not
    as a routine follow-up to an energy.

    Args:
        smiles: The molecule as a SMILES string.
        solvent: Optional implicit solvent name; omit for gas phase.
        symmetry_number: Rotational symmetry number; 1 if the molecule has no symmetry.
        temperature_k: Temperature for the thermal corrections; 0 uses 298.15 K.
        top_bands: How many IR bands to report, strongest first. 0 uses the configured
            default; imaginary modes are always reported in full.

    Returns:
        Frequencies with IR intensities, whether the geometry is a minimum, and the
        thermochemistry with the uncertainty to quote alongside it.
    """

    def _run() -> ThermochemistryResult:
        structure = structure_from_smiles(smiles, multiplicity=None, optimize=True)
        spec = ThermoSpec(
            solvent=solvent,
            symmetry_number=symmetry_number,
            temperature_k=temperature_k or settings.xtb_thermo_temperature_k,
        )
        _, result = relax_to_minimum(structure, OptSpec(solvent=solvent), spec)
        limit = top_bands if top_bands > 0 else settings.xtb_ir_bands_top_n
        # The imaginary mode's 3N-vector is refinement machinery, not something a model can read;
        # the frequency itself is already in `imaginary_frequencies_cm`.
        return result.model_copy(
            update={"modes": result.strongest_bands(limit), "imaginary_displacement": None}
        )

    return await asyncio.to_thread(_run)


@server.tool()
async def predict_developability_profile(smiles: str) -> DescriptorProfile:
    """Compute a developability descriptor panel: MW, LogP, TPSA, H-bond counts, Ro5/Veber flags.

    Use this to triage a candidate before committing bench time — Lipinski's Rule-of-Five
    (`lipinski_violations`) and Veber's rule (`veber_pass`) are widely used oral-bioavailability
    heuristics, not developability verdicts. Report them as flags to weigh alongside everything
    else known about the molecule, never as a pass/fail gate on their own.

    Args:
        smiles: The molecule as a SMILES string.

    Returns:
        The descriptor panel plus the two rule-of-thumb flags.
    """
    return await asyncio.to_thread(compute_descriptor_profile, DescriptorInput(smiles=smiles))


@server.tool()
async def predict_logd(smiles: str, ph: float | None = None) -> LogdResult:
    """Predict the pH-dependent distribution coefficient (logD) of a singly-ionisable molecule.

    Answers "how lipophilic is this at the pH I actually work at?" — useful for HPLC
    mobile-phase pH selection, extraction, and formulation, where the pH-independent LogP alone
    is not the number that matters.

    Built on `predict_pka`, but its domain is **strictly narrower** than that tool's rather than
    the same, so a working pKa is not a promise of a logD. `predict_pka` reports one pKa and one
    Henderson-Hasselbalch term consumes exactly one, so this is defined only where a single
    equilibrium describes the molecule at the pH asked for.

    Served: one O-H/S-H acid (carboxylic acid, phenol, alcohol, thiol) **or** one aromatic/aryl
    nitrogen base — bases are supported and corrected in the opposite direction, which is why the
    result names the site. Further sites are fine while they stay un-ionised at that pH, so a
    diol or sugar (pKa ~15) is served at any ordinary pH and a diacid is served well below its
    pKa.

    Refused, with an error naming the reason rather than a guess: aliphatic amines and
    charged or unparseable inputs (inherited from `predict_pka`); anything **amphoteric**, an
    acid site plus a base site, since `predict_pka` always answers with the acid and never
    evaluates the base; and any **polyprotic** molecule substantially ionised at that pH. The
    second pKa is not computable here at all, so the alternative would be a number wrong by 2-5
    log units carrying a ±1.6 uncertainty. Relay the refusal; do not fall back to logP or retry
    at a pH chosen to get past it.

    Each call runs the full xTB pKa, so asking for several pH values costs one pKa each. Ask for
    the pH that matters.

    Args:
        smiles: The molecule as a SMILES string.
        ph: The pH to evaluate at. Defaults to 7.4 (physiological pH) if omitted.

    Returns:
        logD at the given pH, plus the LogP and pKa it was derived from and the pKa model's
        uncertainty (state it — this is not an exact value).
    """
    return await asyncio.to_thread(_predict_logd, LogdInput(smiles=smiles, ph=ph))
