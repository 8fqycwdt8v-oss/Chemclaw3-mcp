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

## Two callers, both of them machines

The eight tools above take a SMILES and answer a chemist's question. Below them sit the
**primitives**: structure-in, structure-out calculations that Chemclaw3's durable-job activities
compose into reaction energetics, relaxed scans, conformer ensembles and interaction energies.

**No model reads either group.** Chemclaw3 keeps its own `calc` bundle and its own agent-facing tool
surface, and reaches this server from inside `science/calc/store.py::cached_compute` and from
Temporal activities — so this manifest never goes on `CHEMCLAW_CONNECTORS_DIR`, which
`servers/calc/README.md` and `docs/integration.md` already forbid for a different reason (a partial
surface would win the `calc` name collision and remove six stateful tools and every durable job from
the agent). The number of tools declared here is therefore invisible to any prompt, and the
"orchestrator, not chemist" markers on the primitives name **which caller each is written for**, not
a choice a model is being asked to make.

That the primitives could only ever reach a model by way of that already-forbidden wiring is one
more consequence of an existing rule rather than a caveat of its own. (Belt and braces if it ever
came to it: `endpoint.tools` in a bundle manifest is an allowlist, passed to the client as
`allowed_tools` by `connectors/registry.py`, so a surface *can* be narrowed per tool. That is not
what makes the count free here — not being on the agent surface at all is.)

The eight nonetheless carry Chemclaw3's model-facing docstrings word for word, and that is
deliberate: they mirror the tools over there that a model *does* read, so a divergence in what the
two claim is visible in a diff rather than only in an answer.

The split between the groups is by *runtime*, not by subject: **Chemclaw3 keeps orchestration and
the cache; this server holds the physics.** A composite — optimise, take a Hessian, displace along
the imaginary mode, repeat — is a loop with state whose key names its own output, so it cannot be
looked up before it runs and does not belong here. Its parts each key cleanly and do.

That is why `compute_thermochemistry` is **not** on this server. See `servers/calc/README.md`.

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
from typing import Any

from mcp.server.fastmcp import FastMCP

from chemclaw_mcp_calc.engine import crest_cli, crest_search, xtb_atomic, xtb_props
from chemclaw_mcp_calc.engine.config import settings
from chemclaw_mcp_calc.engine.descriptors import (
    DescriptorInput,
    DescriptorProfile,
    compute_descriptor_profile,
)
from chemclaw_mcp_calc.engine.identity import CalculationIdentity, calculation_identity
from chemclaw_mcp_calc.engine.key import Keyed
from chemclaw_mcp_calc.engine.logd import LogdInput, LogdResult
from chemclaw_mcp_calc.engine.logd import predict_logd as _predict_logd
from chemclaw_mcp_calc.engine.pka import PkaInput, PkaResult
from chemclaw_mcp_calc.engine.pka import calc_version as pka_calc_version
from chemclaw_mcp_calc.engine.pka import predict_pka as _predict_pka
from chemclaw_mcp_calc.engine.scan import scan_point_inputs
from chemclaw_mcp_calc.engine.solubility import (
    SolubilityInput,
    SolubilityResult,
)
from chemclaw_mcp_calc.engine.solubility import (
    predict_solubility as _predict_solubility,
)
from chemclaw_mcp_calc.engine.structure import Structure, structure_from_smiles
from chemclaw_mcp_calc.engine.xtb import XtbInput, XtbResult, run_xtb
from chemclaw_mcp_calc.engine.xtb_atomic import AtomicDescriptorResult, SurfacePotentialResult
from chemclaw_mcp_calc.engine.xtb_hessian import HessianSpec, pack_array
from chemclaw_mcp_calc.engine.xtb_hessian import compute_hessian as compute_hessian_engine
from chemclaw_mcp_calc.engine.xtb_opt import (
    OptimizationResult,
    OptimizationSummary,
    OptSpec,
    optimization_inputs,
    optimize_structure,
)
from chemclaw_mcp_calc.engine.xtb_props import (
    ElectronicProperties,
    FukuiMode,
    SiteReactivityResult,
)
from chemclaw_mcp_calc.engine.xtb_spec import XtbSpec

server = FastMCP("calc")

logger = logging.getLogger(__name__)

__all__ = [
    "calculation_key",
    "combine_structures",
    "compute_electronic_properties",
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
async def calculation_key(tool: str, arguments: dict[str, Any]) -> CalculationIdentity:
    """Return what a calculation *would* be stored under, without running it. Cheap; no SCF.

    **This tool is for the caller's cache, not for a chemist.** It answers "have I already computed
    this?" so a client holding a calculation store can look the answer up before paying for it —
    every other tool here is minutes of CPU with nothing cached underneath, so the difference
    between one cheap round trip and one full calculation is the whole point.

    An agent answering a chemist's question should call the compute tool directly. Every compute
    result already carries `calc_version` and `calc_key`, so nothing here has to be called first.

    Args:
        tool: The name of the compute tool whose identity you want — one of the nine on this server.
        arguments: The arguments you would pass to it. `smiles` is required; everything else takes
            that tool's own default. An argument name the tool does not take is **refused rather
            than ignored**, because ignoring one would return the key of a different calculation and
            the lookup would then hit a real answer to a question nobody asked.

    Returns:
        `calc_version` always; `key` (the four fields a store lookup takes) and `calc_key` (the same
        identity as one flat string) where the key is derivable from the arguments; `structure_id`
        for the calculations that run on a geometry; and `caveat` explaining the absence for the two
        that have no key — `predict_logd`, which was never cached, and `compute_thermochemistry`,
        whose key names the relaxed geometry its refinement loop settles on and is therefore an
        output of the calculation rather than a function of its arguments.
    """
    return await asyncio.to_thread(calculation_identity, tool, arguments)


@server.tool()
async def compute_xtb_energy(smiles: str, charge: int = 0) -> XtbResult:
    """Compute the GFN2-xTB total energy of a molecule (fast, semiempirical).

    Runs a quick GFN2-xTB single point. Semiempirical is the ceiling here — there is no
    higher-accuracy method behind this tool to escalate to.

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
        return xtb_props.compute_properties(*xtb_props.properties_inputs(smiles, solvent))

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
        result = xtb_props.compute_fukui(*xtb_props.fukui_inputs(smiles), mode)
        limit = top_n if top_n > 0 else settings.xtb_fukui_top_n
        return result.model_copy(update={"sites": result.sites[:limit]})

    return await asyncio.to_thread(_run)


@server.tool()
async def compute_atomic_descriptors(
    smiles: str, solvent: str | None = None
) -> AtomicDescriptorResult:
    """Per-atom polarisability, dispersion and multipole descriptors (GFN2-xTB, binary only).

    Answers two questions a partial charge cannot: which atom is **polarisable** — a soft,
    dispersion-driven or halogen-bonding site — and how anisotropic its own electron density is.
    For where the electrostatic potential is most positive or negative, which is where a halogen's
    sigma-hole shows up, call `compute_surface_potential`: that is a second calculation with its
    own cost and its own cache entry, not an argument to this one.

    Read it as a per-atom panel, not a ranking. Nothing in it is normalised per molecule, so unlike
    Fukui indices these values *are* comparable between molecules — an iodine's 33.7 au
    polarisability is larger than a fluorine's wherever it sits.

    **This tool needs the `xtb` binary and refuses by name when the deployment has none.** It does
    not approximate: the in-process library exposes no atomic multipoles and no polarisability at
    all, so there is nothing to fall back to. Use `compute_electronic_properties` for partial
    charges, bond orders and frontier orbitals, and `predict_site_reactivity` for site rankings —
    neither needs the binary.

    Args:
        smiles: The molecule as a SMILES string. Must be closed-shell.
        solvent: Optional ALPB implicit solvent name; omit for gas phase.

    Returns:
        One entry per atom, in the same order `compute_electronic_properties` and
        `predict_site_reactivity` use, so the panels join on atom index for one structure. Atomic
        units throughout.
    """

    def _run() -> AtomicDescriptorResult:
        return xtb_atomic.compute_atomic_descriptors(*xtb_atomic.atomic_inputs(smiles, solvent))

    return await asyncio.to_thread(_run)


@server.tool()
async def compute_surface_potential(
    smiles: str, solvent: str | None = None
) -> SurfacePotentialResult:
    """Where a molecule's electrostatic potential is most positive and most negative (GFN2-xTB).

    The two extrema on a molecular surface, in kcal/mol. The **maximum** is where an electrophilic
    patch sits — an acidic hydrogen, or a heavy halogen's sigma-hole, which is what makes a halogen
    bond and which a partial charge cannot show at all. The **minimum** marks the most electron-rich
    patch: a lone pair, a pi face.

    These are extrema over a grid, not a map: use them to compare analogues (does the bromo
    congener still have a positive sigma-hole?), not to locate a patch in space.

    **Needs the `xtb` binary and refuses by name where a deployment has none**, like
    `compute_atomic_descriptors`. It is a separate calculation from that one and costs its own
    single point — an `--esp` run cannot also produce the atomic multipoles, which was measured
    rather than assumed — so ask for it when the question is about the surface.

    Args:
        smiles: The molecule as a SMILES string. Must be closed-shell.
        solvent: Optional ALPB implicit solvent name; omit for gas phase.

    Returns:
        The minimum and maximum potential in kcal/mol and how many grid points they were taken over.
    """

    def _run() -> SurfacePotentialResult:
        return xtb_atomic.compute_surface_potential(*xtb_atomic.surface_inputs(smiles, solvent))

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
        return OptimizationSummary.of(optimize_structure(*optimization_inputs(smiles, solvent)))

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


# ------------------------------------------------------------------------------------------------
# The primitives.
#
# Everything below takes and returns a `Structure` rather than a SMILES, and exists so Chemclaw3's
# durable-job activities can compose the physics they need while keeping the orchestration — and the
# cache — on their side. An agent answering a chemist's question wants the eight tools above.
#
# Each is separately keyed, which is the property that makes the composition worth doing: Chemclaw3
# caches one row per *primitive* instead of one per job, so a solvent screen that adds a seventh
# solvent reuses the other six, and a scan that gains two points reuses the twenty-four it had.


class HessianPayload(Keyed):
    """Second derivatives at one geometry, with the arrays base64-encoded as `.npy`.

    `hessian_npy` is (3N, 3N) in Hartree/Angstrom^2. Exactly one of `dipole_derivatives_npy`
    (3N, 3) in Debye/Angstrom and `ir_intensities` (one per Cartesian mode, km/mol) is populated,
    and which one says which backend ran: the in-process path collects dipole derivatives while it
    displaces, the `xtb` binary computes intensities itself.

    **Both are what a caller needs to derive an IR spectrum**, and neither is a spectrum: the
    normal-mode projection and the RRHO arithmetic over them stayed in Chemclaw3, because they are
    pure partition functions over what this returns.

    `max_gradient_hartree_per_angstrom` is the evidence that the geometry was a stationary point —
    see the field comment. Optional rather than required, so a row written before it existed is
    still a complete row and `CALCULATION_EPOCH` does not have to move for it.
    """

    structure_id: str
    method: str
    solvent: str | None
    atom_count: int
    electronic_energy_hartree: float
    # Largest absolute gradient component at the geometry that was differentiated. A Hessian
    # describes the surface *around* a point, and only at a stationary point do its eigenvalues mean
    # frequencies — so this is what lets a caller assert what it has rather than assume it. `None`
    # from the `xtb` binary, which reports no gradient beside its Hessian.
    max_gradient_hartree_per_angstrom: float | None = None
    hessian_npy: str
    dipole_derivatives_npy: str | None = None
    ir_intensities: list[float] | None = None


class EnsemblePayload(Keyed):
    """What one CREST search found, and nothing computed from it.

    `members` is ordered lowest energy first and carries each structure with the **rotamer
    degeneracy** that collapsed onto it. The degeneracy is not bookkeeping: a Boltzmann population
    that ignores it is wrong by a lot — measured on n-butane, degeneracy-weighted populations give
    the anti conformer 59.2% against CREST's own 59.14%, and ignoring degeneracy gives 73%.

    Populations, conformational entropy and the ensemble free-energy correction are arithmetic over
    exactly these two numbers per member, and they stayed with the durable jobs that report them.
    """

    structure_id: str
    method: str
    solvent: str | None
    search: str
    effort: str
    members: list[crest_cli.EnsembleMember]
    total_found: int


@server.tool()
async def embed_structure(
    smiles: str,
    charge: int | None = None,
    multiplicity: int | None = None,
    relax_with_force_field: bool = True,
) -> Structure:
    """Build the 3D geometry every other primitive takes. Cheap — RDKit only, no SCF.

    **For the orchestrator, not the chemist.** It is the first call of any composition, because a
    `Structure` is what the primitives consume and what their keys are derived from — and because
    the embedding must happen *here*: `structure_id` is a hash of coordinates RDKit produced at
    *this* RDKit version from the configured seed, so a geometry embedded anywhere else would
    address a different row.

    Canonicalises the SMILES before embedding, which is what makes two spellings of one molecule
    produce one geometry and therefore one key.

    Args:
        smiles: The molecule as a SMILES string.
        charge: Net charge. Omit to take the SMILES' own formal charge; a value contradicting it is
            refused rather than computed at the wrong electron count.
        multiplicity: Spin multiplicity 2S+1, validated against the electron count. Omit for the
            closed-shell default; pass 0 to derive it from the SMILES' explicit radical electrons,
            which is what a set of species that may include radicals wants.
        relax_with_force_field: Pre-optimise with MMFF where it has parameters. On by default and
            worth leaving on — measured over five textbook isomer pairs, a raw ETKDG embedding gets
            the *sign* of the relative energy wrong in two of them.

    Returns:
        The geometry, its content address (`structure_id`), and the canonical SMILES it came from.
    """
    # `multiplicity=0` is the wire spelling of "derive it from the radicals": the engine takes
    # `None` for that, and `None` on this signature already means "use the closed-shell default".
    derived = None if multiplicity == 0 else (1 if multiplicity is None else multiplicity)
    return await asyncio.to_thread(
        structure_from_smiles,
        smiles,
        charge=charge,
        multiplicity=derived,
        optimize=relax_with_force_field,
    )


@server.tool()
async def combine_structures(
    first: Structure, second: Structure, separation_angstrom: float = 3.5
) -> Structure:
    """Place two molecules side by side as one structure. Cheap — pure geometry, no SCF.

    **For the orchestrator, not the chemist.** It is the step that produces the *subject* a
    non-covalent complex search is keyed on: without it a caller cannot derive that key and would be
    back to guessing one.

    Each monomer is centred and offset along x by the sum of their radii plus the gap, so the pair
    starts apart regardless of shape. Only a starting point — the search's wall potential decides
    where they end up.

    **Not symmetric in its arguments.** Swapping them is a different starting arrangement and a
    different key, so order the pair canonically first if A-with-B and B-with-A should be one
    calculation.

    **Refuses an open-shell monomer.** Two doublets are a singlet or a triplet, and which one is a
    chemical decision rather than an arithmetic identity — so a radical pair is refused here instead
    of being handed the high-spin state silently, with every energy downstream of it computed on a
    surface nobody chose.

    Args:
        first: The monomer held at the origin. Must be closed-shell.
        second: The monomer offset along x. Must be closed-shell.
        separation_angstrom: Gap between the two bounding spheres.

    Returns:
        The pair as one structure, charges summed, closed-shell.
    """
    return await asyncio.to_thread(
        crest_search.combine_structures, first, second, separation_angstrom
    )


@server.tool()
async def relax_structure(
    structure: Structure,
    solvent: str | None = None,
    frozen_atoms: list[int] | None = None,
) -> OptimizationResult:
    """Relax a geometry to a stationary point, and return the coordinates.

    **For the orchestrator, not the chemist** — `optimize_geometry` is the same calculation for an
    agent, summarised without its 3N Cartesians. This one hands the geometry back because the next
    primitive in a composition needs it.

    Raises rather than returning a non-converged geometry: frequencies, thermochemistry and reaction
    energies computed on one all look ordinary and mean nothing, so holding a result is a guarantee
    that the gradient criterion was met.

    Args:
        structure: The starting geometry, from `embed_structure` or a previous primitive.
        solvent: ALPB implicit solvent name; omit for gas phase.
        frozen_atoms: Indices held at their input positions — an exact constrained minimisation over
            the free subspace, not a penalty. Empty for a free optimisation.

    Returns:
        The converged energy, the relaxed geometry, how far the atoms moved, and the key this
        calculation is addressed by. The geometry carries `origin`, that same key, so its lineage
        travels with it.
    """
    spec = OptSpec(solvent=solvent, frozen_atoms=tuple(frozen_atoms or ()))
    return await asyncio.to_thread(optimize_structure, spec, structure)


@server.tool()
async def compute_properties_at(
    structure: Structure, solvent: str | None = None
) -> ElectronicProperties:
    """One GFN2-xTB single point at a given geometry: energy, orbitals, charges, bond orders.

    **For the orchestrator, not the chemist** — `compute_electronic_properties` embeds from a SMILES
    and answers the same question. This one runs at whatever geometry it is handed, which is what a
    composition needs: an energy at a *relaxed* geometry is a different number from one at a
    force-field geometry, and only the caller knows which it has.

    The energy comes out of the same SCF as everything else here, so this is also the cheapest way
    to get one — there is no separate energy-only primitive because there would be nothing cheaper
    about it.

    Args:
        structure: The geometry to evaluate at.
        solvent: ALPB implicit solvent name; omit for gas phase.

    Returns:
        Total energy, HOMO/LUMO/gap in eV, dipole in Debye, per-atom Mulliken charges and Wiberg
        bond orders, plus the key this calculation is addressed by.
    """
    return await asyncio.to_thread(
        xtb_props.compute_properties, XtbSpec(task="properties", solvent=solvent), structure
    )


@server.tool()
async def compute_fukui_at(
    structure: Structure,
    mode: FukuiMode = "electrophilic",
    solvent: str | None = None,
    top_n: int = 0,
) -> SiteReactivityResult:
    """Condensed Fukui indices at a given geometry — regioselectivity on a shape you already hold.

    **For the orchestrator, not the chemist** — `predict_site_reactivity` embeds from a SMILES and
    answers the same question. This one runs at whatever geometry it is handed, which is what a
    composition needs: a flexible molecule's site ranking is a property of the conformer, so
    averaging it over an ensemble means asking for it *per conformer*, and only the caller holds
    those geometries.

    Reads exactly like its twin: a hypothesis about electronic susceptibility within one molecule,
    never between molecules, with sterics and the specific reagent outside the model. The three
    single points do not depend on `mode` — it only chooses the sort — so the result carries all
    three indices per atom and a second ranking is a re-sort rather than three more SCFs.

    Args:
        structure: The geometry to evaluate at. Must be closed-shell; the ions of an open-shell
            parent could be either of two spin states, and picking one silently would be guessing.
        mode: Which attack to rank for.
        solvent: ALPB implicit solvent name; omit for gas phase.
        top_n: How many atoms to return, most susceptible first. 0 uses the configured default.

    Returns:
        The ranked sites with all three Fukui indices per atom, and the key this calculation is
        addressed by — the same `xtb.fukui` row `predict_site_reactivity` would produce at this
        geometry.
    """

    def _run() -> SiteReactivityResult:
        result = xtb_props.compute_fukui(XtbSpec(task="fukui", solvent=solvent), structure, mode)
        limit = top_n if top_n > 0 else settings.xtb_fukui_top_n
        return result.model_copy(update={"sites": result.sites[:limit]})

    return await asyncio.to_thread(_run)


@server.tool()
async def compute_hessian(structure: Structure, solvent: str | None = None) -> HessianPayload:
    """Second derivatives at a geometry — the expensive half of every vibrational question.

    **For the orchestrator, not the chemist.** It returns matrices, not a spectrum: the normal-mode
    projection, the IR intensities and the RRHO thermochemistry over them are pure arithmetic and
    stayed in Chemclaw3 with the jobs that report them.

    Cost is 6N + 1 single points in-process — minutes on a drug-sized molecule, and refused above
    `CHEMCLAW_XTB_HESSIAN_MAX_ATOMS` (150 by default) because a tool call that would run for an hour
    is a durable job. A run that gets past that and still exceeds
    `CHEMCLAW_XTB_INLINE_TIMEOUT_SECONDS` (900 s, the manifest's own budget) is stopped rather than
    left burning CPU for an answer nobody is waiting for. **Nothing is cached here**, so ask
    `calculation_key` first.

    **It differentiates whatever geometry it is handed** — a transition state and a scan point are
    legitimate subjects, so this is not a refusal — and returns
    `max_gradient_hartree_per_angstrom` beside the matrix so the caller can tell a minimum from an
    unrelaxed embedding. Frequencies from a non-stationary geometry look entirely ordinary and mean
    nothing, and a geometry displaced along a soft direction shows no imaginary mode at all.

    The arrays are base64-encoded `.npy`, which round-trips float64 exactly and is byte-for-byte
    what Chemclaw3's artifact store already holds. At the 150-atom cap a response is about 2.2 MB.

    Args:
        structure: The geometry to differentiate at — normally a converged minimum from
            `relax_structure`, because a Hessian at a non-stationary point describes nothing.
        solvent: ALPB implicit solvent name; omit for gas phase.

    Returns:
        The Hessian in Hartree/Angstrom^2, the electronic energy and the largest gradient component
        at that geometry, and either the dipole derivatives (in-process backend) or the binary's own
        per-mode IR intensities.
    """
    spec = HessianSpec(solvent=solvent)

    def _run() -> HessianPayload:
        hessian = compute_hessian_engine(spec, structure)
        resolved = spec.for_structure(structure)
        key = resolved.cache_key(structure)
        return HessianPayload(
            calc_version=key.calc_version,
            calc_key=key.as_str(),
            structure_id=structure.structure_id,
            method=resolved.method,
            solvent=resolved.solvent,
            atom_count=len(structure.elements),
            electronic_energy_hartree=hessian.electronic_energy_hartree,
            max_gradient_hartree_per_angstrom=hessian.max_gradient,
            hessian_npy=pack_array(hessian.matrix),
            dipole_derivatives_npy=(
                None
                if hessian.dipole_derivatives is None
                else pack_array(hessian.dipole_derivatives)
            ),
            ir_intensities=(
                None
                if hessian.ir_intensities is None
                else [float(value) for value in hessian.ir_intensities]
            ),
        )

    return await asyncio.to_thread(_run)


@server.tool()
async def scan_point(
    structure: Structure, atoms: list[int], value: float, solvent: str | None = None
) -> OptimizationResult:
    """One point of a relaxed scan: drive an internal coordinate, freeze it, relax the rest.

    **For the orchestrator, not the chemist.** A profile is a *sweep* over values, and the sweep is
    a loop the caller writes — which is the point: Chemclaw3 caches one row per point instead of one
    per profile, so adding two points to a twenty-four-point scan recomputes two.

    The decomposition is exact rather than convenient: the sweep drives every point from the input
    geometry rather than from the previous one, deliberately, so the points were already
    independent.

    A scan point is an ordinary constrained optimisation and keys as one, so it shares a row with a
    `relax_structure` call that froze the same atoms at the same geometry.

    Args:
        structure: The starting geometry. Must carry its SMILES — connectivity is what lets RDKit
            move the attached fragment rather than dragging one atom out of place.
        atoms: Two atoms for a bond, three for an angle, four for a dihedral, bonded in sequence.
        value: Angstrom for a bond, degrees for an angle or dihedral.
        solvent: ALPB implicit solvent name; omit for gas phase.

    Returns:
        The relaxed geometry at that coordinate value and its energy. Relative energies and the
        barrier maximum are arithmetic over a whole profile and belong to whoever walked it.
    """

    def _run() -> OptimizationResult:
        return optimize_structure(*scan_point_inputs(structure, tuple(atoms), value, solvent))

    return await asyncio.to_thread(_run)


@server.tool()
async def search_conformer_ensemble(
    structure: Structure,
    search: crest_search.EnsembleSearch = "conformers",
    effort: crest_cli.CrestEffort = "quick",
    solvent: str | None = None,
    temperature_k: float = 0.0,
) -> EnsemblePayload:
    """Sample conformers, tautomers or protomers with CREST. Minutes to hours; one call, one answer.

    **For the orchestrator, not the chemist**, and the one primitive that cannot be decomposed: a
    metadynamics search is a single stateful trajectory, its intermediate structures are not
    answers, and there is no point at which half of it is a result.

    It removes the caveat on every other number here — everything else describes *one* conformer,
    which for a flexible molecule is a shape rather than the molecule. `tautomers` is the search
    that matters most: a pKa, a Fukui ranking and a reaction energy all describe whichever tautomer
    was drawn, so getting it wrong invalidates them silently.

    Returns the ensemble and nothing computed from it. Boltzmann populations, conformational entropy
    and the ensemble free-energy correction are arithmetic over the energies and degeneracies here.

    **The shipped image carries `crest` 3.0.2, so this runs here.** A deployment that replaced or
    trimmed that image gets a refusal naming the binary rather than a single-conformer answer
    dressed up as an ensemble — degrading is the one thing this call will not do.

    Args:
        structure: The starting geometry.
        search: Which space to sample.
        effort: How hard. `quick` is right for a screening question; `extensive` for the case where
            a missed conformer changes the answer.
        solvent: ALPB implicit solvent name; omit for gas phase. temperature_k: Sampling
        temperature, passed to `crest --temp`. 0 uses the configured default.

    Returns:
        Every member found, lowest energy first, each with its rotamer degeneracy.
    """
    spec = crest_search.EnsembleSpec(
        search=search,
        effort=effort,
        solvent=solvent,
        temperature_k=temperature_k or settings.xtb_thermo_temperature_k,
    )
    return await asyncio.to_thread(_ensemble_payload, spec, structure, search)


@server.tool()
async def search_binding_modes(
    structure: Structure,
    effort: crest_cli.CrestEffort = "quick",
    solvent: str | None = None,
) -> EnsemblePayload:
    """Search how two molecules associate, with CREST's non-covalent mode. Minutes to hours.

    **For the orchestrator, not the chemist.** Takes the *combined* pair from `combine_structures`,
    because the pair is the calculation's subject exactly as a single molecule is elsewhere — and
    because that is what the key is derived from.

    The `--nci` wall potential is what makes this a binding search: without it a metadynamics run
    simply lets two molecules drift apart instead of sampling how they bind.

    Returns the binding modes and nothing computed from them. An interaction energy is the bound
    complex's energy minus the two relaxed monomers' — three `relax_structure` calls and a
    subtraction, on the caller's side, each separately cached.

    **One mode found is a weak result, not a confident one**: it usually means the search was too
    quick rather than that the pair has a single way to bind.

    **The shipped image carries `crest` 3.0.2** — see `search_conformer_ensemble` for what a
    trimmed image gets instead.

    Args:
        structure: The combined pair, from `combine_structures`.
        effort: How hard to search.
        solvent: ALPB implicit solvent name; omit for gas phase.

    Returns:
        Every binding mode found, lowest energy first.
    """
    spec = crest_search.ComplexSpec(effort=effort, solvent=solvent)
    return await asyncio.to_thread(_ensemble_payload, spec, structure, "complex")


def _ensemble_payload(
    spec: crest_search.EnsembleSpec | crest_search.ComplexSpec,
    structure: Structure,
    search: str,
) -> EnsemblePayload:
    """Run a CREST search and wrap it with the identity it is addressed by.

    One helper for both search tools because the wrapping is identical and the two differ only in
    which spec they build — the difference that matters (`ComplexSpec` keys the surrounding
    optimisations' backend as well) lives in the specs, not here.
    """
    members = crest_search.search_ensemble(spec, structure)
    key = spec.cache_key(structure)
    return EnsemblePayload(
        calc_version=key.calc_version,
        calc_key=key.as_str(),
        structure_id=structure.structure_id,
        method=spec.method,
        solvent=spec.solvent,
        search=search,
        effort=spec.effort,
        members=members,
        total_found=len(members),
    )
