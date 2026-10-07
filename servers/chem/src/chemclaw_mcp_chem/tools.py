"""The `chem` MCP tool surface: bench chemistry over RDKit.

Tool docstrings are the prompt and every model call pays for them, so they state the rule (units,
what the tool is not, which index to pass, the bound by name); the evidence lives beside the code
or in tests. Every tool is a pure function of its arguments plus a vendored table.

The enumerations produce the set Chemclaw3's `rank_species` and `survey_bond_strengths` rank, so a
model never invents one; each is priced by an input bound before it runs, except
`enumerate_stereoisomers`. RDKit work holds the GIL, so each tool runs it in `asyncio.to_thread`:
that keeps the event loop and `/healthz` answering, but buys no throughput — the server scales by
replicas (`engine/admission.py`).
"""

from __future__ import annotations

import asyncio
import functools
import os
from collections.abc import Awaitable, Callable, Coroutine
from typing import Annotated, Any, ParamSpec, TypeVar

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent
from mcp_server_kit.limits import env_bound

from chemclaw_mcp_chem.engine import stoichiometry
from chemclaw_mcp_chem.engine.admission import (
    ADMISSION_MARKER,
    DEFAULT_MAX_CONCURRENT_HEAVY_CALLS,
    RETIRED_VARIABLE,
    VARIABLE,
    Admission,
)
from chemclaw_mcp_chem.engine.cleavage import CleavageMode, CleavageSet, enumerate_cleavages
from chemclaw_mcp_chem.engine.depiction import render_svg
from chemclaw_mcp_chem.engine.reagents import (
    ResolvedCompound,
    UnrecognisedCompound,
    describe_miss,
    resolve_compound_name,
)
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
from chemclaw_mcp_chem.engine.substitution import (
    SubstitutionMode,
    SubstitutionSet,
    enumerate_substitution_set,
)
from chemclaw_mcp_chem.engine.torsions import Torsion, enumerate_torsion_candidates

server = FastMCP("chem")

# The pod's ceiling on concurrent heavy calls, built at import (a test replaces the attribute).
# `env_bound` refuses a bad value naming the variable. Written as a literal rather than `VARIABLE`
# because the fleet's bound scan reads the literal; `tests/test_admission.py` holds the two equal.
if os.environ.get(RETIRED_VARIABLE, "").strip():
    raise ValueError(
        f"{RETIRED_VARIABLE} was renamed {VARIABLE} when the ceiling grew from depictions to every "
        f"heavy chem tool; set {VARIABLE} instead, or unset both for the default of "
        f"{DEFAULT_MAX_CONCURRENT_HEAVY_CALLS}. It is refused rather than ignored so a deployment "
        "does not run on the default while believing its own number."
    )
_admission = Admission(
    env_bound(
        "CHEMCLAW_CHEM_MAX_CONCURRENT_HEAVY_CALLS",
        default=DEFAULT_MAX_CONCURRENT_HEAVY_CALLS,
        minimum=1,
        consequence="this pod would refuse every depiction and species enumeration it is asked for",
    )
)

_P = ParamSpec("_P")
_T = TypeVar("_T")


def _admitted(work: Callable[_P, Awaitable[_T]]) -> Callable[_P, Coroutine[Any, Any, _T]]:
    """Bound how many heavy calls run at once, refusing promptly when the pod is full.

    Stamped with `ADMISSION_MARKER` for the coverage test. `asyncio.shield` releases the slot when
    the work finishes, not when the caller stops waiting, since the worker thread keeps running.
    `functools.wraps` lets FastMCP read the real signature for the argument schema.
    """

    @functools.wraps(work)
    async def _guarded(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        return await _admission.admit(
            work(*args, **kwargs), lambda: _admission.acquire(work.__name__)
        )

    setattr(_guarded, ADMISSION_MARKER, True)
    return _guarded


@server.tool()
async def resolve_compound(name: str) -> Annotated[CallToolResult, ResolvedCompound | None]:
    """Resolve a reagent name, abbreviation, or SMILES to its canonical structure.

    Use this whenever the chemist names a reagent in words ("DIPEA", "Pd(dppf)Cl2", "2-MeTHF")
    before calling a tool that takes a structure.

    **It knows a small table of bench reagents, not compound names in general** — solvents, bases,
    catalysts, ligands, coupling agents, oxidants. Substrates and building blocks ("aniline",
    "4-bromoanisole") are not in it and nothing looks a name up elsewhere: pass those as SMILES.

    An unrecognised name comes back as `recognised: false` with the reason and, for a near-miss,
    `suggestions`. Say it was not resolved; never guess a structure, and treat a suggestion as a
    question for the chemist, not a substitution.

    **A formula that is also a valid SMILES is refused** (`CO` is carbon monoxide to a chemist and
    methanol to the parser; likewise `NO`, `CN`, bare element symbols): the error names both
    readings, so pass the structure you meant.

    **A metal-ligand bond comes back charge-separated** (`...[Pd-]...[NH2+]...`), the same compound
    as its dative spelling, and resolving it again returns it unchanged. Pass the returned SMILES on
    exactly as written; only an alkene or arene π-bound to a metal keeps its arrow.

    Args:
        name: What the chemist wrote — a trivial name, an abbreviation, or a SMILES string.

    Returns:
        The canonical structure with the name it was recognised as, or the explicit miss.

    Raises:
        ValueError: the name is one of the formula/SMILES collisions above.
    """
    # An unrecognised name falls through to an RDKit canonicalisation attempt, so this is not the
    # dictionary lookup it looks like.
    answer = await asyncio.to_thread(_resolve_or_explain, name)
    return _as_tool_result(answer)


def _resolve_or_explain(name: str) -> ResolvedCompound | UnrecognisedCompound:
    """The resolution, or the worded miss — both off the event loop in one hop."""
    resolved = resolve_compound_name(name)
    return describe_miss(name) if resolved is None else resolved


def _as_tool_result(answer: ResolvedCompound | UnrecognisedCompound) -> CallToolResult:
    """Put an answer on the wire with the declared structured shape and a text the agent can read.

    FastMCP turns `None` into no content at all, so a miss gets words in the text block. The
    structured half stays `null` for a miss, matching the output schema Chemclaw3 was given.
    """
    text = answer.model_dump_json(indent=2)
    structured = answer.model_dump(mode="json") if isinstance(answer, ResolvedCompound) else None
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent={"result": structured},
    )


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

    Answers "for 250 g of starting material at 1.2 equiv of base in 10 volumes of THF, what do I
    charge?" from molecular weights and densities.

    **A charge specified in volumes goes in `solvents`/`volumes`**, never converted to equivalents
    yourself — that conversion is the error this argument pair exists to remove. Nothing checks
    which path a substance takes: pass it in the units it was *specified* in (acetic acid at 1.5
    equiv or DMF as the Vilsmeier reagent go in `reagents`); each row's `role` reports which.

    Args:
        basis: The limiting reagent (name or SMILES); its mass sets the scale.
        basis_mass_g: Mass of the limiting reagent charged, in grams.
        reagents: Species charged by molar equivalent (names or SMILES), in order.
        equivalents: Molar equivalents for each entry of `reagents`, same order and length.
        solvents: Species charged by volume (names or SMILES), in order.
        volumes: Process volumes for each entry of `solvents`, in mL per g of basis, same order and
            length. A 4:1 THF/water mixture at 10 total volumes is `[8.0, 2.0]`.

    Returns:
        One row per species with the amount in mmol (`moles_mmol`) and the mass in g, and for
        solvents density (g/mL) and volume (mL). Unresolvable reagents are listed in `unresolved`
        with no row, never a guessed mass. A formula/SMILES collision (`CO`, a bare element), an
        unresolvable solvent or a solvent with no density on file is an error, because a silently
        dropped row flatters every mass metric derived from the table.
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

    Compares routes or conditions on waste, not only yield. Pair it with `stoichiometry_table`,
    whose `mass_g` column is this input. E-factor is kg waste per kg product (Sheldon); PMI is kg
    total input per kg product; they differ by exactly 1. Lower is better for both.

    Args:
        input_masses_g: Every charged species' mass in grams — reagents, catalyst **and solvent**
            (every row of the charge table). Omitting solvent is how these numbers get flattered.
        product_mass_g: Isolated product mass in grams. Must be positive, and not above the total
            input — an unsound mass balance is refused.

    Returns:
        Both metrics plus the masses behind them.
    """
    # Arithmetic over a list of floats, with no RDKit in it: the one tool here that would gain
    # nothing from a worker thread, so it does not take one.
    return stoichiometry.green_metrics(input_masses_g, product_mass_g)


@server.tool()
@_admitted
async def render_structure(smiles: str, highlight_atoms: list[int] | None = None) -> str:
    """Draw a molecule or reaction as an SVG the chat surface can show inline.

    Use it when a structure is the answer. **To show a set of structures, prefer a `structures`
    artefact where `create_exhibit` is available**: the chat draws those itself, so no SVG passes
    through your context.

    **Use `highlight_atoms` to show the bond you are about to rotate** — pass the `atoms` of an
    `enumerate_torsions` entry so the chemist can check the choice before a scan is paid for.
    Indices address the string you pass here, so pass the *same* SMILES they came from (for
    `describe_sites`, its returned canonical `smiles`); an index into a different spelling lands
    on another atom and looks like confirmation.

    **A drawing that would not fit is refused, not cut down** — above `MAX_DEPICTION_ATOMS` (250
    atoms by default) or `MAX_DEPICTION_CHARS` (50,000 characters of SVG). Highlighting roughly
    doubles the SVG, so drop the highlights or draw a fragment.

    Args:
        smiles: A molecule SMILES, or a reaction SMILES (`reactants>>products`).
        highlight_atoms: Atom indices to mark, with the bonds between them. Molecules only.

    Returns:
        An inline SVG document.

    Raises:
        ValueError: undrawable structure, an index outside it, or the atom or character ceiling
            exceeded — the message names which and what to do instead.
    """
    return await asyncio.to_thread(render_svg, smiles, highlight_atoms)


@server.tool()
async def enumerate_torsions(smiles: str) -> list[Torsion]:
    """List the rotatable bonds of a molecule, so one can be *named* rather than indexed.

    **Call this before any torsion scan or rotational-barrier job, and never work out torsion
    indices yourself.** An index is not a name: `(4, 5)` is the amide C-N in one spelling of a
    compound and a ring bond in another, and the scan would return a plausible barrier for the
    wrong bond with no error.

    **Then confirm the bond before spending anything.** One match for what the chemist named:
    proceed and say which, by `label`. Several: ask, listing the labels. None: say so, and list
    what there is.

    A graph operation — no calculation and no cache.

    Args:
        smiles: The molecule, as SMILES.

    Returns:
        One entry per symmetry-distinct rotatable bond. Carry `torsion_id` (stable however the
        molecule is written) rather than indices across turns. `atoms` is the dihedral to scan,
        `period_degrees` the range to cover, `equivalent_bonds` the copies needing no scan. Ring
        bonds are not listed. `kind="top"` (methyl, tert-butyl) and `kind="xh"` (O-H, S-H, N-H)
        carry no dihedral atoms: a top's energy is already in the free-rotor treatment, but an X-H
        rotation is neither covered nor scannable here — say it is not answered.
    """
    return await asyncio.to_thread(enumerate_torsion_candidates, smiles)


@server.tool()
async def describe_sites(smiles: str) -> SiteSet:
    """Name every atom of a molecule, so a per-atom number can be reported as a *position*.

    **Call this before, or alongside, any per-atom calculation** — site reactivity, partial charges,
    bond orders, C-H abstraction. Those return an atom index, and an index is not a name; never
    work the mapping out yourself from the SMILES.

    **One entry per symmetry class**: toluene's two *ortho* carbons are one site. Report the class,
    and treat the spread of a per-atom value across its members as the noise floor.

    A graph operation and a table of SMARTS — no calculation and no cache.

    Args:
        smiles: The molecule, as SMILES.

    Returns:
        `smiles`: the canonical form the indices are numbered against — usually **not** the string
        you passed. Use this one for `render_structure` or a per-atom calculation. Then `sites`, one
        per symmetry-distinct heavy atom: `site_id` is stable however the molecule is written (carry
        it across turns); `atoms` are the heavy-atom indices and `hydrogens` the indices its
        hydrogens take once made explicit (a C-H question is read on the hydrogen, reported on the
        carbon); `scopes` tags which questions the site answers (filter on e.g. `ring_carbons`);
        `label`, `ring_position` and `adjacent_ring_heteroatoms` make an answer sayable. Hydrogens
        are not sites of their own.
    """
    return await asyncio.to_thread(describe_atom_sites, smiles)


@server.tool()
@_admitted
async def describe_topology(smiles: str) -> Topology:
    """Say what the molecular graph is like, before spending an expensive search on it.

    Structural — no quantum calculation, and nothing here is a prediction. Ask it first when unsure
    whether an expensive search is worth it: the commonest waste is a conformer search on a rigid
    molecule — and it answers for molecules the enumerations refuse. It is **not** free:
    `tautomer_count` is an enumeration, up to about a second on a large molecule. Above
    `MAX_TAUTOMER_HEAVY_ATOMS` (500 heavy atoms by default) tautomers are not counted and every
    other field is still answered.

    How to read the answer:

    - **`rotatable_bonds` near zero**: a conformer search will find little; one shape is the
      ensemble.
    - **`tautomer_count` 1**: no tautomer question. Above 1, resolve the form *before* computing
      anything else. **Null** with `tautomer_count_saturated`: more than the cap, emphatically
      tautomeric — not the number 64. Null with `tautomer_count_computed` false: too large to
      count, says nothing either way; ask about the tautomeric unit on its own.
    - **`unassigned_stereocentres` 0**: a stereoisomer expansion returns one structure.
    - **`ionisable_acidic_sites` / `ionisable_basic_sites`**: one site means `predict_pka` covers
      it; several, or both kinds, is the polyprotic/amphoteric case for a microspecies profile.

    Args:
        smiles: The molecule, as SMILES.

    Returns:
        Counts from the graph — deliberately not a recommendation.
    """
    return await asyncio.to_thread(describe_molecule, smiles)


@server.tool()
@_admitted
async def enumerate_tautomers(smiles: str) -> SpeciesSet:
    """List the tautomers of a molecule — the proton-shift isomers it can exist as.

    Structural: says which forms are possible, not which dominates — pass `smiles` from the result
    to `rank_species` for that. Use it before any other calculation on a molecule with a mobile
    proton between heteroatoms (azole N-H, purines, 1,3-dicarbonyls, amidines, 2-pyridone), since
    every property is a property of one form.

    A molecule above `MAX_TAUTOMER_HEAVY_ATOMS` (500 heavy atoms by default) is refused naming the
    bound; ask about the tautomeric unit on its own.

    Args:
        smiles: The molecule, as SMILES.

    Returns:
        The tautomers, canonical and de-duplicated, input first. Refuses rather than truncating
        past 64 — a partial set would normalise a downstream population over part of the universe.
    """
    return await asyncio.to_thread(enumerate_tautomer_set, smiles)


@server.tool()
@_admitted
async def enumerate_protonation_states(smiles: str) -> SpeciesSet:
    """List the protonation microstates — each ionisable site toggled, one at a time.

    Structural. Which state dominates at a given pH is `rank_species`; for a single site,
    `predict_pka` answers directly and more cheaply.

    Each site is toggled **singly**: the parent plus each single ionisation. Combined states (a
    zwitterion's doubly-ionised form) come from calling this again on a result, so the 2^n
    expansion stays an explicit decision.

    Refused when `ionisable sites x heavy atoms` exceeds `MAX_SITE_ATOM_PRODUCT` (150,000 by
    default) — a bound on the work, not on how many states there are; the refusal names both
    numbers and three ways on. Also refused past 32 microstates.

    Args:
        smiles: The molecule, as SMILES. Give the neutral form where there is one.

    Returns:
        The microstates, input first, each labelled with the site that moved.
    """
    return await asyncio.to_thread(enumerate_microstates, smiles)


@server.tool()
@_admitted
async def enumerate_stereoisomers(smiles: str) -> SpeciesSet:
    """List the stereoisomers of a molecule at the centres its SMILES leaves *unassigned*.

    Structural. `rank_species` on the result answers the diastereomer question, never the
    enantiomer one — enantiomers are isoenergetic and no calculation here distinguishes them.

    **Only unassigned centres are expanded**: defined stereochemistry is a claim, so a
    fully-specified input comes back as itself.

    Refused before any isomer is built when `isomers x heavy atoms` exceeds
    `MAX_STEREO_ISOMER_ATOM_PRODUCT` (6,000 by default), or past 64 isomers either way: assign the
    centres the question does not turn on, or ask about the stereogenic fragment alone.

    Args:
        smiles: The molecule, as SMILES.

    Returns:
        The isomers. An input with open centres is *not* among them — it is the underspecified
        question, not a member of the set.
    """
    return await asyncio.to_thread(enumerate_stereoisomer_set, smiles)


@server.tool()
async def enumerate_bond_cleavages(smiles: str, mode: CleavageMode = "homolytic") -> CleavageSet:
    """List every breakable bond and the two fragments breaking it would give.

    Structural and cheap. Pass `cleavages` from the result to `survey_bond_strengths`, which
    computes one balanced reaction per bond and answers "which bond breaks first".

    Acyclic single bonds only (breaking a ring bond gives a biradical, not two fragments), and
    symmetry-equivalent bonds collapse to one entry. Refused past `MAX_CLEAVAGES` (48) distinct
    bonds: name the bonds that matter, or ask about a fragment.

    Args:
        smiles: The molecule, as SMILES.
        mode: `homolytic` for radicals (bond strength, H-abstraction, autoxidation) or
            `heterolytic` for the ion pair. The two are not comparable; never mix them in a survey.

    Returns:
        One entry per distinct bond, fragments carrying explicit radical electrons or charges.
        `atoms` number `parent` (canonical) once its hydrogens are made explicit — heavy atoms keep
        `parent`'s indices and hydrogens follow — not the SMILES you passed.
    """
    return await asyncio.to_thread(enumerate_cleavages, smiles, mode)


@server.tool()
@_admitted
async def enumerate_degradants(smiles: str) -> DegradantSet:
    """Propose degradation products by applying forced-degradation transforms to the structure.

    **Candidates to screen, not a prediction or a ranking** — each entry says a transform matches
    the graph, not that the chemistry happens; report them so. Each names its transform, which is
    what a chemist can argue with ("N-oxidation" on a hindered amine). The transforms are the
    oxidative, hydrolytic and thermal routes an ICH Q1A study looks for first; the list is short and
    not comprehensive: a degradant it does not propose is one nobody is offered.

    Refused past 64 proposals, or when `matches x heavy atoms` exceeds
    `MAX_DEGRADANT_MATCH_ATOM_PRODUCT` (100,000 by default), naming both numbers — ask about the
    repeat unit instead.

    Args:
        smiles: The parent compound, as SMILES.

    Returns:
        The proposals; group them by `condition` when reporting. The parent is not among them.
    """
    return await asyncio.to_thread(enumerate_degradant_candidates, smiles)


@server.tool()
@_admitted
async def enumerate_substitutions(
    smiles: str, substituent: str | None = None, mode: SubstitutionMode = "move"
) -> SubstitutionSet:
    """List the regioisomers of a substitution series: the set a "which position" question ranks.

    Structural. Pass `smiles` and `labels` from the result to `rank_species` (as `species` and
    `labels`, `ranking="custom"`) to rank the isomers by free energy.

    - `move` (default): each substituent on an aromatic carbon is moved, one at a time, to every
      other aromatic C-H of its ring system. The input is first, labelled "as given". `substituent`
      restricts which group moves (`C` methyl, `OC` methoxy); omit it to move every group.
    - `add`: `substituent` is put on each symmetry-distinct aromatic C-H once. The input is **not**
      in the result — it has a different formula.

    `substituent` bonds through its first atom (`OC` methoxy, `CO` hydroxymethyl) or a single `*`
    (nitro is `*[N+](=O)[O-]`).

    **A ranking of this set is not a regioselectivity.** `rank_species` orders finished isomers by
    stability, while electrophilic aromatic substitution is usually decided kinetically at the sigma
    complex: say so whenever you report one. Not covered: aliphatic positions, a second
    substitution (call again on a result), and a group on a ring nitrogen — for azole
    N-alkylation, move the carbon substituents instead.

    Refused above `MAX_SUBSTITUTION_HEAVY_ATOMS` (250 heavy atoms by default) or when `candidates x
    heavy atoms` exceeds `MAX_SUBSTITUTION_CANDIDATE_ATOM_PRODUCT` (20,000 by default).

    Args:
        smiles: The molecule, as SMILES.
        substituent: The group to move (`move`, optional) or add (`add`, required), as SMILES.
        mode: `move` for positional isomers of the input, `add` for one new substitution.

    Returns:
        `smiles` and `labels`, positions named as `describe_sites` names them, and `sites` giving
        each landing position's `site_id` on `parent`. Refuses rather than truncating past 64
        isomers.
    """
    return await asyncio.to_thread(enumerate_substitution_set, smiles, substituent, mode)
