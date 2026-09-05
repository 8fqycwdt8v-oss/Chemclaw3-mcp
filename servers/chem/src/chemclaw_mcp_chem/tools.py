"""The `chem` MCP tool surface: bench chemistry over RDKit.

**These docstrings are the prompt.** Argument names, defaults and this prose are what the agent
reads before deciding whether to call a tool and what to pass it, and they are carried over from
Chemclaw3's own `chem` connector word for word — several sentences in them exist because a live run
got something wrong in a way that was measured, and shortening one would delete the measurement.

Every capability here is a pure function of its arguments plus a read of a vendored table: no
store, no durable state, no network. A count is deliberately not written here — the sentence that
used to say "five" outlived two additions.

**The six enumerations exist so that the expensive half never has to guess its own universe.**
Chemclaw3's `rank_species` and `survey_bond_strengths` rank a *set*; these produce the set from the
molecular graph, for free. Its skills state the rule as *enumerate, then compute, and never the
reverse*, and the reason is that the alternative is a model inventing plausible SMILES.

**"Cheap" is relative to a DFT job, not to an event loop.** RDKit parsing, `Descriptors.MolWt` and
especially 2D-coordinate generation plus SVG rendering are CPU-bound C++ that holds the GIL for
milliseconds to tens of milliseconds, and one process answers every connected chat turn on one
loop — Chemclaw3 load-tested this connector and measured throughput flat from 10 to 50 concurrent
users, the signature of exactly that. So each tool does its RDKit work in a worker thread
(`asyncio.to_thread`) and the coroutine only awaits it.

**What the offload buys is latency isolation, not throughput, and this docstring used to claim the
opposite.** It said RDKit releases the GIL for the heavy passes "so the threads are real parallelism
on a multi-CPU pod". Measured here, 1 to 16 concurrent depictions of a 241-atom molecule on a
four-core box ran at cpu_util 0.80-1.15x with wall clock scaling linearly and throughput flat at
16-23/s: they run one at a time. The offload is still right and still necessary — it is what keeps
the event loop and `/healthz` answering while a render runs — but the way this server serves more
depictions per second is more pods. `engine/admission.py` has the measurement and what follows from
it; `deploy/hpa.yaml` is the remedy.
"""

from __future__ import annotations

import asyncio
import functools
import os
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, ParamSpec, TypeVar

from mcp.server.fastmcp import FastMCP

from chemclaw_mcp_chem.engine import stoichiometry
from chemclaw_mcp_chem.engine.admission import (
    ADMISSION_MARKER,
    DEFAULT_MAX_CONCURRENT_RENDERS,
    Admission,
)
from chemclaw_mcp_chem.engine.cleavage import CleavageMode, CleavageSet, enumerate_cleavages
from chemclaw_mcp_chem.engine.depiction import render_svg
from chemclaw_mcp_chem.engine.reagents import ResolvedCompound, resolve_compound_name
from chemclaw_mcp_chem.engine.sites import SiteSet, describe_atom_sites
from chemclaw_mcp_chem.engine.species import (
    DegradantSet,
    SpeciesSet,
    Topology,
    describe_molecule,
    enumerate_degradant_candidates,
    enumerate_microstates,
    enumerate_stereoisomer_set,
    enumerate_tautomer_set,
)
from chemclaw_mcp_chem.engine.torsions import Torsion, enumerate_torsion_candidates

server = FastMCP("chem")

# The pod's ceiling on concurrent depictions. Built at import; a test that needs a different ceiling
# replaces this attribute, so the number a gate enforces is the number it was built from. The
# default and its derivation live in `engine/admission.py`, beside the measurement they rest on.
_admission = Admission(
    int(os.environ.get("CHEMCLAW_CHEM_MAX_CONCURRENT_RENDERS", str(DEFAULT_MAX_CONCURRENT_RENDERS)))
)

_P = ParamSpec("_P")
_T = TypeVar("_T")


def _release_slot(task: asyncio.Task[Any]) -> None:
    """Give the slot back when the *work* finishes, not when whoever asked for it stops waiting.

    Retrieving the exception is not tidiness: a shielded task whose awaiter was cancelled has nobody
    left to receive its failure, and asyncio logs "exception was never retrieved" at exit for each
    one — noise in the logs of exactly the incident this gate exists for.
    """
    _admission.release()
    if not task.cancelled():
        task.exception()


def _admitted(work: Callable[_P, Awaitable[_T]]) -> Callable[_P, Coroutine[Any, Any, _T]]:
    """Bound how many depictions run at once, refusing promptly when the pod is full.

    Applied under `@server.tool()` so the served callable is the guarded one, and stamped with
    `ADMISSION_MARKER` so a coverage test can check the gated set rather than a second hand-kept
    list. `asyncio.shield` releases the slot when the work finishes rather than when the caller
    stops waiting: cancelling the awaiting coroutine does not stop the worker thread, so releasing
    on cancellation would hand a slot to a retry while the original render kept burning a core.

    `functools.wraps` is load-bearing rather than polite: FastMCP builds each tool's argument schema
    from `inspect.signature`, which follows `__wrapped__` back to the real signature. Without it the
    tool would advertise `(*args, **kwargs)`.
    """

    @functools.wraps(work)
    async def _guarded(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        _admission.acquire(work.__name__)
        task = asyncio.ensure_future(work(*args, **kwargs))
        task.add_done_callback(_release_slot)
        return await asyncio.shield(task)

    setattr(_guarded, ADMISSION_MARKER, True)
    return _guarded


@server.tool()
async def resolve_compound(name: str) -> ResolvedCompound | None:
    """Resolve a reagent name, abbreviation, or SMILES to its canonical structure.

    Use this whenever the chemist names a reagent in words ("DIPEA", "Pd(dppf)Cl2", "2-MeTHF")
    before calling any tool that needs a SMILES — the property calculators, the similarity search,
    and the substructure search all take structures, not names.

    Returns `None` when the name is not recognised. That is a real answer: say the reagent is not
    in the known set rather than guessing a structure, because a wrong structure would silently
    corrupt every downstream calculation and search.

    **A formula that is also a valid SMILES is refused, not resolved.** `CO`, `NO`, `CN` and every
    bare element symbol read as one substance to a chemist and another to the parser — `CO` is
    carbon monoxide and is the SMILES for methanol — so the error names both readings and you pass
    the structure you meant. Only you know which it was; guessing put 30.61 g of methanol into a
    carbonylation charge list.

    Args:
        name: What the chemist wrote — a trivial name, an abbreviation, or a SMILES string.

    Returns:
        The canonical structure with the name it was recognised as, or `None` if unknown.

    Raises:
        ValueError: the name is one of the reviewed formula/SMILES collisions above.
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
        `unresolved` and carry no row — never a guessed mass. A species whose name is both a
        formula and a SMILES (`CO`, `NO`, a bare element) is an error rather than an `unresolved`
        entry, because the two readings differ in molecular weight. A solvent that cannot be
        resolved, or
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
@_admitted
async def render_structure(smiles: str, highlight_atoms: list[int] | None = None) -> str:
    """Draw a molecule or reaction as an SVG the chat surface can show inline.

    Use this when a structure is the answer, or when naming several related structures in prose
    would be ambiguous — a chemist reads a drawing far faster than a SMILES string.

    **Use `highlight_atoms` to show which bond you are about to rotate.** Pass the `atoms` of an
    `enumerate_torsions` entry and the chemist sees the torsion drawn, which is the one form in
    which they can check the choice before a scan is paid for. Saying "the amide C-N" is a claim;
    the picture is the evidence.

    **An index is only an atom of the string you pass here.** Pass the *same* SMILES the indices
    came from: `enumerate_torsions` numbers the string it was given, while `describe_sites` numbers
    the canonical form and returns it as `smiles` — pass that one. An index that addresses a
    different spelling is still in range, so the highlight lands on some other atom and looks like
    confirmation.

    **A drawing that would not fit is refused, not cut down.** The SVG grows with the molecule —
    ethanol is about 2,000 characters, a drug substance around 35,000, a 250-atom chain 126,000 —
    and highlighting roughly doubles it, so the two levers when a structure is refused are dropping
    the highlights and drawing a fragment instead of the whole thing. Nothing is truncated, because
    half an SVG document draws nothing at all while still costing what it costs to read.

    Args:
        smiles: A molecule SMILES, or a reaction SMILES (`reactants>>products`).
        highlight_atoms: Atom indices to mark, and the bonds between them. Molecules only, and
            numbered as `smiles` numbers them.

    Returns:
        An inline SVG document.

    Raises:
        ValueError: the structure is not drawable, an index does not address one of its atoms, the
            molecule is above the atom ceiling, or the finished drawing is above the character
            ceiling — the message names which, and what to do instead.
    """
    return await asyncio.to_thread(render_svg, smiles, highlight_atoms)


@server.tool()
async def enumerate_torsions(smiles: str) -> list[Torsion]:
    """List the bonds of a molecule that can be rotated, so one can be *named* rather than indexed.

    **Call this before any torsion scan or rotational-barrier job.** Those take four atom indices,
    and an index is not a name: measured on RDKit, `(4, 5)` is the amide C-N of
    `c1ccc(NC(C)=O)cc1` and an aromatic *ring* bond of `CC(=O)Nc1ccccc1` — the same compound,
    rewritten. The scan would run and return a plausible barrier for a different bond, with no
    error anywhere. Never work out torsion indices yourself; take them from here.

    **Then confirm which one before spending anything.** If the chemist named a bond in words and
    exactly one entry matches, proceed and say back which bond you chose, by its `label`. If
    several match, ask which — listing the labels. If none matches, say so and list what there is.
    A guessed torsion is the one failure here that looks exactly like an answer.

    Free: a graph operation, no calculation, no cache. So enumerate first, look at what the
    molecule actually has, and only then decide what to spend.

    Args:
        smiles: The molecule, as SMILES.

    Returns:
        One entry per symmetry-distinct rotatable bond. `torsion_id` is a handle that stays the
        same however the molecule is written — carry it, not the indices, when a later turn asks
        about "the same bond". `atoms` is the dihedral to scan and `period_degrees` is the range a
        scan has to cover; `equivalent_bonds` names the copies that need no separate scan. Ring
        bonds are not listed, because driving one is a ring pucker rather than a rotation.

        Two kinds carry **no** dihedral atoms, because the rotating end holds only hydrogens and a
        hydrogen index means something only inside one explicit-H numbering. They are not the same
        answer. `kind="top"` is a methyl or tert-butyl rotation, and its energy really is already
        carried by the free-rotor treatment of the low modes — the reason to list it at all is that
        the rotatable-bond descriptor everyone reaches for reports zero for toluene. `kind="xh"` is
        an O-H, S-H or N-H rotation: acetamide's amide N-H is 16-18 kcal/mol and acetic acid's
        syn/anti O-H is 5-6, neither is in the low modes, and neither can be scanned from here.
        Say it is not answered rather than that it is accounted for.
    """
    return await asyncio.to_thread(enumerate_torsion_candidates, smiles)


@server.tool()
async def describe_sites(smiles: str) -> SiteSet:
    """Name every atom of a molecule, so a per-atom number can be reported as a *position*.

    **Call this before, or alongside, any per-atom calculation** — site reactivity, partial charges,
    bond orders, a C-H abstraction question. Those return an atom *index*, and an index is not a
    name: a chemist asks which ring position is nitrated, and nothing in an index says. Never work
    the mapping out yourself from the SMILES; take it from here. This is `enumerate_torsions`' rule
    one dimension down, and for the same measured reason.

    **One entry per symmetry class, not per atom.** Toluene's two *ortho* carbons are one site,
    asked once. That grouping is the point rather than a tidy-up: measured on phenol, the two
    *ortho* carbons' Fukui indices differ by 0.0088 purely because the planar O-H makes one *syn*
    and the other *anti* — the same size as the *ortho*-to-*meta* difference somebody would draw a
    conclusion from. Report the class, and use the spread across its members as the noise floor.

    Free: a graph operation and a table of SMARTS. No calculation, no cache, no network. So ask it
    first, see what positions the molecule actually has, and only then decide what to spend.

    Args:
        smiles: The molecule, as SMILES.

    Returns:
        `smiles` — the canonical form the indices below are numbered against, which is **not** the
        string you passed unless you already had it canonical. Pass that one to `render_structure`
        or to a per-atom calculation; measured on 2,5-dichloropyridine written `c1cc(Cl)ncc1Cl`,
        the site at atom 4 is an aromatic carbon of the canonical form and the ring nitrogen of the
        string as typed, and a highlight drawn on the typed string confirms a different atom.
        Then one `sites` entry per symmetry-distinct heavy atom. `site_id` is a handle that stays
        the same
        however the molecule is written — carry it, not the indices, when a later turn asks about
        "the same position". `atoms` are the heavy-atom indices of the class and `hydrogens` the
        indices its hydrogens carry once a calculator makes them explicit, which is the join key
        for a C-H question: the ranking is read on the hydrogen and reported on the carbon.
        `scopes` says which questions the site is a candidate answer to, so a ring-substitution
        question can ask for `ring_carbons` instead of sifting a list where the answer sits behind
        the heteroatom and four hydrogens. `label`, `ring_position` and `adjacent_ring_heteroatoms`
        are what make an answer sayable — the last being what separates the two chlorines of a
        dichloropyrimidine, since both are *ortho* to a ring nitrogen and only one sits between two.
        Hydrogens are not sites of their own, by the same rule that gives a symmetric top no
        dihedral: a hydrogen index means something only inside one explicit-H numbering.
    """
    return await asyncio.to_thread(describe_atom_sites, smiles)


@server.tool()
async def describe_topology(smiles: str) -> Topology:
    """Say what the molecular graph is like, before spending anything on it.

    Free and structural — no calculation runs. Ask this first when you are unsure whether an
    expensive search is worth it, because the commonest waste in this catalogue is paying for a
    conformer search to discover the molecule was rigid.

    How to read the answer:

    - **`rotatable_bonds` near zero** means a conformer search will find little. A rigid molecule
      has one shape and `compute_electronic_properties` on it is already the ensemble answer.
    - **`tautomer_count` of 1** means there is no tautomer question; resolving it would rank a set
      of one. Above 1, resolve the form *before* computing anything else about the molecule,
      because every downstream number describes whichever form was assumed. **Null** means more
      than the cap, with `tautomer_count_saturated` saying so — it is emphatically tautomeric, and
      it is not the number 64.
    - **`unassigned_stereocentres` of 0** means a stereoisomer expansion returns one structure.
    - **`ionisable_acidic_sites` and `ionisable_basic_sites`**: one of either means `predict_pka`
      covers the question; both, or several, is the amphoteric/polyprotic case a microspecies
      profile exists for.

    Args:
        smiles: The molecule, as SMILES.

    Returns:
        Counts from the graph. Deliberately not a recommendation — the numbers are stable, and what
        to do about them is judgement.
    """
    return await asyncio.to_thread(describe_molecule, smiles)


@server.tool()
async def enumerate_tautomers(smiles: str) -> SpeciesSet:
    """List the tautomers of a molecule — the proton-shift isomers it can exist as.

    Free and structural. Pass `smiles` from the result straight to `rank_species` to find out which
    form actually dominates; this tool says only which forms are possible.

    Use it before any other calculation on a molecule with a mobile proton between heteroatoms:
    heterocyclic N-H (pyrazoles, imidazoles, triazoles, purines), 1,3-dicarbonyls, amidines,
    2-pyridone/2-hydroxypyridine. Every property of a tautomeric molecule is a property of one
    *form*, so computing before resolving describes whichever form happened to be drawn.

    Args:
        smiles: The molecule, as SMILES.

    Returns:
        The tautomers, canonical and de-duplicated, with the input first. Refuses rather than
        truncating past 64 — a partial set would make a downstream population normalize over a
        fraction of the universe while looking complete.
    """
    return await asyncio.to_thread(enumerate_tautomer_set, smiles)


@server.tool()
async def enumerate_protonation_states(smiles: str) -> SpeciesSet:
    """List the protonation microstates — each ionisable site toggled, one at a time.

    Free and structural. The ranking that says which dominates at a given pH is `rank_species`;
    for a single site, `predict_pka` answers directly and more cheaply.

    Each ionisable site is toggled **singly**: the parent, plus each single ionisation. Combined
    states (a zwitterion's doubly-ionised form, say) are reachable by calling this again on a
    result, which keeps the expansion an explicit decision rather than a silent 2^n.

    Args:
        smiles: The molecule, as SMILES. Give the neutral form where there is one.

    Returns:
        The microstates with the input first, each labelled with the site that moved.
    """
    return await asyncio.to_thread(enumerate_microstates, smiles)


@server.tool()
async def enumerate_stereoisomers(smiles: str) -> SpeciesSet:
    """List the stereoisomers of a molecule at the centres its SMILES leaves *unassigned*.

    Free and structural. `rank_species` on the result gives the relative free energies — which is
    the diastereomer question; it is not the enantiomer question, since enantiomers are isoenergetic
    and no calculation here distinguishes them.

    **Only unassigned centres are expanded.** A structure drawn with defined stereochemistry is a
    claim, and re-enumerating over it would offer the enantiomer of a compound somebody specified.
    A fully-specified input therefore comes back as itself.

    Args:
        smiles: The molecule, as SMILES.

    Returns:
        The isomers. Note that an input with open centres is *not* among them — it is the
        underspecified question the set answers, not a member of it.
    """
    return await asyncio.to_thread(enumerate_stereoisomer_set, smiles)


@server.tool()
async def enumerate_bond_cleavages(smiles: str, mode: CleavageMode = "homolytic") -> CleavageSet:
    """List every breakable bond and the two fragments breaking it would give.

    Free and structural. Pass `cleavages` from the result straight to `survey_bond_strengths`,
    which computes one balanced reaction per bond — that is the expensive half, and it is what
    answers "which bond breaks first".

    Acyclic single bonds only: breaking one bond of a ring gives an open-chain biradical rather
    than two fragments, which is not a dissociation energy in the sense the question means.
    Symmetry-equivalent bonds are collapsed to one entry, so a methyl contributes one C-H rather
    than three identical calculations.

    Args:
        smiles: The molecule, as SMILES.
        mode: `homolytic` for radicals (the usual question — bond strength, H-abstraction,
            autoxidation), `heterolytic` for the ion pair. The two are not comparable; a survey
            mixing them ranks nothing meaningful.

    Returns:
        One entry per distinct bond, with fragments whose radical electrons or charges are explicit
        so the calculation needs no separately declared spin state. `parent` is the canonical form
        the molecule was enumerated on, and each `atoms` pair numbers *that* molecule with its
        hydrogens made explicit — not the SMILES you passed, unless it was already canonical.
    """
    return await asyncio.to_thread(enumerate_cleavages, smiles, mode)


@server.tool()
async def enumerate_degradants(smiles: str) -> DegradantSet:
    """Propose degradation products by applying forced-degradation transforms to the structure.

    Free and structural, and **a short list rather than a ranking or a prediction**: each entry
    says a transform *matches* the molecule's graph, not that the chemistry happens. Report it as
    candidates to screen, and say so — a transform can match a substructure the chemistry does not
    favour.

    Each candidate names the transform that produced it, which is the half a chemist can argue
    with: "N-oxidation" can be rejected for a hindered amine, and a bare SMILES cannot.

    The transforms are the oxidative, hydrolytic and thermal routes an ICH Q1A forced-degradation
    study looks for first. The list is deliberately short and named; it is not comprehensive, and a
    degradant it does not propose is one nobody is offered.

    Args:
        smiles: The parent compound, as SMILES.

    Returns:
        The proposals, grouped by nothing — group them by `condition` when reporting. The parent
        is deliberately not among them, so `count` reads as "how many liabilities" rather than one
        more.
    """
    return await asyncio.to_thread(enumerate_degradant_candidates, smiles)
