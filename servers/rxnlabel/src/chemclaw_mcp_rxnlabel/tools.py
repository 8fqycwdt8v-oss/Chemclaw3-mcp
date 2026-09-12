"""The `rxnlabel` MCP tool surface: what a reaction is made of, and what it is called.

**These docstrings are the prompt** — for the one caller that is a model. In practice the caller is
Chemclaw3's background labelling drain, which calls the two *batch* tools and nothing else: a
multi-million-row corpus at one round trip per reaction is a multi-million round trips, and at a
batch of two hundred it is a few tens of thousands. The single-reaction tools exist for a person
asking about one reaction, and for the fleet's own dev harness.

**Everything here is honest about absence.** RXNMapper and Rxn-INSIGHT are optional extras — the
image installs them, a developer's checkout does not — and each answer says what it could not
compute rather than guessing. What makes that safe rather than a silent quality gradient is
`labeller_version`: it names which components were present, so rows labelled without one go stale
the moment a deployment installs it and the corpus repairs itself.

**Nothing here is cached.** The caller's row *is* the cache — a label is stored against a reaction
and re-derived only when the version moves — and a second cache in front of that would answer from
a superseded labeller while the row said it was stale.

The work is CPU-bound and holds the GIL: RDKit SMARTS matching over a few dozen species, and a
transformer forward pass where the mapper is installed. So every tool offloads to a worker thread,
the call `chem` makes for the same reason.
"""

from __future__ import annotations

import asyncio
import functools
import os
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from typing import Any, ParamSpec, TypeVar

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from chemclaw_mcp_rxnlabel.engine import mapping, naming, roles, species, version
from chemclaw_mcp_rxnlabel.engine.admission import (
    ADMISSION_MARKER,
    DEFAULT_MAX_CONCURRENT_BATCHES,
    Admission,
)

server = FastMCP("rxnlabel")

# One request may carry at most this many reactions. The bound exists because the request body is
# already capped in bytes by the transport, and a body of ten thousand one-line reactions is under
# that cap and is minutes of transformer time — a timeout the caller reads as an outage rather than
# as "ask for less". Measured on the RDKit-only path at 2.8 ms/reaction, so 500 is 1.4 s of one
# core before a mapper is installed and more after.
#
# Read from the environment for the reason every other bound in this fleet is: a magic number is a
# bound nobody can loosen for a genuinely larger drain without editing code, and being readable is
# also what puts it in `tests/test_fleet.py`'s derived inventory of what a deployment can move.
MAX_BATCH = int(os.environ.get("CHEMCLAW_RXNLABEL_MAX_BATCH", "500"))

# The pod's ceiling on concurrent labelling, built at import like the batch bound above; a test that
# needs a different ceiling replaces this attribute rather than the variable, because the number a
# gate enforces and the number it was built from must be the same number.
_admission = Admission(
    int(
        os.environ.get(
            "CHEMCLAW_RXNLABEL_MAX_CONCURRENT_BATCHES", str(DEFAULT_MAX_CONCURRENT_BATCHES)
        )
    )
)

_P = ParamSpec("_P")
_T = TypeVar("_T")


def _release_slots(task: asyncio.Task[Any], *, charge: int) -> None:
    """Give the slots back when the *work* finishes, not when whoever asked for it stops waiting.

    Retrieving the exception is not tidiness: a shielded task whose awaiter was cancelled has nobody
    left to receive its failure, and asyncio logs "exception was never retrieved" at exit for every
    one of them — noise in the logs of exactly the incident this gate exists for.
    """
    _admission.release(charge)
    if not task.cancelled():
        task.exception()


def _batch_slots() -> int:
    """What one labelling call costs, in cores — `mapping.inference_threads()`, never a call count.

    One where no mapper is installed, because the RDKit path is measured GIL-bound; the mapper's
    configured intra-op width where one is, because torch releases the GIL and sizes itself from the
    node rather than from the container. `engine/admission.py` has the measurement and the argument,
    and it is read at call time so an operator and the gate see the same number.
    """
    return mapping.inference_threads()


def _admitted(work: Callable[_P, Awaitable[_T]]) -> Callable[_P, Coroutine[Any, Any, _T]]:
    """Bound how much CPU is labelling at once, refusing promptly when the pod is full.

    Applied under `@server.tool()` so the served callable is the guarded one, and stamped with
    `ADMISSION_MARKER` so `tests/test_admission.py` can check the gated set against the served
    surface instead of against a second hand-kept list here.

    **The slot is released when the work finishes, not when the caller stops waiting**, and
    `asyncio.shield` is what buys that. Cancelling the awaiting coroutine does not stop the worker
    thread underneath it, so releasing on cancellation would hand a slot to the drain's retry while
    the original batch was still burning — which is the precise failure this gate is for.

    `functools.wraps` is load-bearing rather than polite: FastMCP builds each tool's argument schema
    from `inspect.signature`, which follows `__wrapped__` back to the real signature and resolves
    its annotations against *that* function's module. Without it every tool here would advertise
    `(*args, **kwargs)`.
    """

    @functools.wraps(work)
    async def _guarded(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        charge = _admission.acquire(work.__name__, _batch_slots())
        task = asyncio.ensure_future(work(*args, **kwargs))
        task.add_done_callback(functools.partial(_release_slots, charge=charge))
        return await asyncio.shield(task)

    setattr(_guarded, ADMISSION_MARKER, True)
    return _guarded


class SpeciesRepresentation(BaseModel):
    """What one species is, and what it was doing."""

    smiles: str = Field(description="The canonical form of the structure as given, or as given.")
    role: str = Field(
        description=(
            "One of starting-material, product, reagent, solvent, catalyst, ligand, base, "
            "additive, unknown. `unknown` means this structure was not found in the reaction — "
            "not that it could not be classified."
        )
    )
    scaffold: str | None = Field(
        default=None, description="Bemis-Murcko scaffold; null for an acyclic molecule."
    )
    functional_groups: list[str] = Field(
        default_factory=list,
        description=(
            "Groups from this server's own vocabulary. Stable across deployments whether or not "
            "the optional extras are installed, because it is queried by exact name. Empty means "
            "the molecule was read and carries none of them — **unless** this species is named in "
            "the reaction's `unreadable_species`, which is what tells the two apart."
        ),
    )


class ReactionRepresentation(BaseModel):
    """One reaction: its atom map where there is one, and every species it was asked about."""

    id: str
    version: str = Field(
        description=(
            "The labeller this answer came from — store it beside the label. Same string "
            "`labeller_version` returns, carried here so a kept answer can be judged stale "
            "without a second round trip that may race this one."
        )
    )
    reaction_smiles: str = Field(
        description=(
            "The canonical form of the species that could be **read**. A component RDKit cannot "
            "parse is left out of it and named in `unreadable_species` — see that field."
        )
    )
    mapped_smiles: str | None = Field(
        default=None,
        description="Atom-mapped reaction SMILES, or null where no mapper is installed.",
    )
    unreadable_species: list[str] = Field(
        default_factory=list,
        description=(
            "Every species string that could not be read, from the reaction and from the list you "
            "sent — verbatim, so you can see what it was. Empty is the normal case and means the "
            "reaction above is complete. This is the difference between a partial answer and a "
            "wrong one: an unreadable species is dropped from `reaction_smiles` and comes back "
            "with an empty `functional_groups`, which otherwise reads as 'carries none'."
        ),
    )
    species: list[SpeciesRepresentation] = Field(
        default_factory=list, description="Positional against the species list that was sent."
    )


class ReactionNaming(BaseModel):
    """One reaction's classification. Every field null where nothing matched."""

    id: str
    version: str = Field(
        description=(
            "The labeller this answer came from — store it beside the label, for the reason "
            "`ReactionRepresentation.version` gives."
        )
    )
    named_reaction: str | None = None
    reaction_class: str | None = None
    rxno_id: str | None = Field(
        default=None,
        description=(
            "Always null from this server. Rxn-INSIGHT names reactions in its own vocabulary and "
            "carries no ontology id; inventing one from an unaudited lookup would be worse than "
            "none, because the id is what a caller uses to escape the vocabularies problem."
        ),
    )
    confidence: float | None = Field(
        default=None,
        description=(
            "Always null from this server. A SMIRKS either matched or it did not, and attaching a "
            "number to that would be a confidence about nothing."
        ),
    )
    method: str | None = Field(
        default=None, description="`smirks` where a rule matched, null where none did."
    )


class ReactionRequest(BaseModel):
    """One reaction to represent: an id to answer under, the reaction, and what to classify."""

    id: str = Field(min_length=1)
    reaction_smiles: str = Field(min_length=1, description="`reactants>agents>products`.")
    species: list[str] = Field(
        default_factory=list,
        description=(
            "The structures to assign roles to, in the caller's order. Sent explicitly rather "
            "than parsed out of the reaction, because a caller's ordinals come from its own "
            "record and the reaction string groups the agents together — the two orders differ "
            "on every reaction with a solvent."
        ),
    )


class NamingRequest(BaseModel):
    """One reaction to classify."""

    id: str = Field(min_length=1)
    reaction_smiles: str = Field(min_length=1, description="`reactants>agents>products`.")


class RepresentBatch(BaseModel):
    """What `represent_reactions` answers.

    Keyed by the ids that were sent, and a reaction that could not be represented is simply absent
    rather than present-and-empty — the caller records what it got and leaves the rest for the next
    pass, which is the difference between a partial batch and a failed one.
    """

    version: str
    results: list[ReactionRepresentation] = Field(default_factory=list)


class NameBatch(BaseModel):
    """What `name_reactions` answers."""

    version: str
    results: list[ReactionNaming] = Field(default_factory=list)


class LabellerVersion(BaseModel):
    """What this deployment's labels are stamped with, and what it is made of."""

    version: str = Field(
        description=(
            "The identity a caller stores beside a label. A row whose stored version differs from "
            "this is stale and must be re-labelled — which is how an installed extra, or an "
            "upgraded one, repairs a corpus without anyone marking anything."
        )
    )
    components: dict[str, str] = Field(
        description="What went into it: the server, RDKit, and each optional model or `absent`."
    )


@server.tool()
async def labeller_version() -> LabellerVersion:
    """What this server's labels are stamped with — ask before storing any label from it.

    The caller must not derive this. It names every component whose output survives into a label,
    so a locally-built one would be well-formed and match nothing: every stored row would look
    stale forever and nothing would raise.
    """
    return await asyncio.to_thread(_version)


@server.tool()
@_admitted
async def represent_reaction(
    reaction_smiles: str, species: list[str] | None = None
) -> ReactionRepresentation:
    """Atom-map one reaction and say what each species was doing in it.

    Answers "what is this reaction made of": the atom-mapped reaction, and for every species its
    canonical form, its Bemis-Murcko scaffold, the functional groups it carries, and its role —
    starting material, product, reagent, solvent, catalyst, **ligand**, base or additive.

    The last two are the point. A recorded reaction says "reagent" for a phosphine and for a
    carbonate alike; which of them is the ligand and which the base is decided here, from the
    structures and from the rest of the flask (a phosphine is a ligand when there is a metal to
    bind and a stoichiometric reagent when there is not).

    The answer carries the `version` that produced it — store that beside any label you keep — and
    `unreadable_species`, which names every component RDKit could not parse. Those are dropped from
    `reaction_smiles`, so an empty `unreadable_species` is what says the reaction came back whole.

    Args:
        reaction_smiles: `reactants>agents>products`, agents kept.
        species: The structures to classify, in your order; the answer is positional against it.
            Omit it to classify every species the reaction names, left to right.
    """
    request = ReactionRequest(
        id="1", reaction_smiles=reaction_smiles, species=species or _all_species(reaction_smiles)
    )
    return (await asyncio.to_thread(_represent, [request]))[0]


@server.tool()
@_admitted
async def name_reaction(reaction_smiles: str) -> ReactionNaming:
    """Classify one reaction into a named reaction and a reaction class.

    Answers "what reaction is this": a name a chemist recognises ("Buchwald-Hartwig amination",
    "Heck terminal vinyl") from 527 curated SMIRKS, plus the coarse class it belongs to.

    Every field is null when nothing matched, and that is a real answer rather than a failure —
    most of what a patent corpus contains has no name. It is also null when the optional classifier
    is not installed in this deployment; `labeller_version` is what tells the two apart, and what
    makes the rows re-label once it is.
    """
    request = NamingRequest(id="1", reaction_smiles=reaction_smiles)
    return (await asyncio.to_thread(_name, [request]))[0]


@server.tool()
@_admitted
async def represent_reactions(reactions: list[ReactionRequest]) -> RepresentBatch:
    """`represent_reaction` over a batch — the form a corpus-labelling drain should call.

    At most `CHEMCLAW_RXNLABEL_MAX_BATCH` reactions per request (500 by default). A reaction that
    could not be represented is absent from `results` rather than present and empty, so a caller can
    record what it got and leave the rest.

    This server also bounds how many batches it labels **at once**: past that it refuses promptly,
    naming the ceiling, rather than queueing a batch that would come back after you stopped waiting
    for it. Re-send the identical batch when that happens.
    """
    _check_batch(reactions)
    return RepresentBatch(
        version=_version().version, results=await asyncio.to_thread(_represent, reactions)
    )


@server.tool()
@_admitted
async def name_reactions(reactions: list[NamingRequest]) -> NameBatch:
    """`name_reaction` over a batch — the form a corpus-labelling drain should call.

    At most `CHEMCLAW_RXNLABEL_MAX_BATCH` reactions per request (500 by default). A reaction that
    could not be read is absent from `results`. Past this server's concurrency ceiling the call is
    refused promptly rather than queued; re-send the identical batch.
    """
    _check_batch(reactions)
    return NameBatch(version=_version().version, results=await asyncio.to_thread(_name, reactions))


def _check_batch(reactions: Sequence[object]) -> None:
    """Refuse an oversized batch by saying how much to ask for.

    A `ValueError`, because `mcp_server_kit` re-raises that cause untouched while replacing every
    other exception with an internal-error notice — so this reaches the caller as the worded
    refusal it is, and is classified there as bad data rather than as an outage to retry.
    """
    if len(reactions) > MAX_BATCH:
        raise ValueError(
            f"{len(reactions)} reactions in one request exceeds the batch limit of {MAX_BATCH}; "
            "split the batch — this bound exists because a body of ten thousand reactions is "
            "under the transport's byte cap and is minutes of work, which times out as an outage"
        )


def _version() -> LabellerVersion:
    """The version and its components, computed synchronously."""
    return LabellerVersion(version=version.labeller_version(), components=version.components())


def _represent(reactions: list[ReactionRequest]) -> list[ReactionRepresentation]:
    """Represent each reaction, skipping the ones RDKit cannot read."""
    stamp = _version().version
    answers = []
    for request in reactions:
        canonical = _canonical_reaction(request.reaction_smiles)
        if canonical is None:
            continue
        # **Mapped once.** `roles.assign` needs the map for the reactant-versus-reagent split and
        # the answer carries it as a field; deriving it in both places ran the transformer twice
        # per reaction, which is the cost `MAX_BATCH` was set against.
        mapped = mapping.map_reaction(request.reaction_smiles)
        assigned = roles.assign(request.reaction_smiles, request.species, mapped)
        answers.append(
            ReactionRepresentation(
                id=request.id,
                version=stamp,
                reaction_smiles=canonical,
                mapped_smiles=mapped,
                unreadable_species=_unreadable(request),
                species=[
                    SpeciesRepresentation(
                        smiles=species.canonical_smiles(raw) or raw,
                        role=role,
                        scaffold=species.scaffold(raw),
                        functional_groups=species.functional_groups(raw) or [],
                    )
                    for raw, role in zip(request.species, assigned, strict=True)
                ],
            )
        )
    return answers


def _unreadable(request: ReactionRequest) -> list[str]:
    """Every species string of this request RDKit could not read, in the order it was written.

    Skipping an unreadable species is right and argued (`roles._canonical_set`): a patent extract's
    fiftieth species may be an OCR artefact, and losing the other forty-nine over it is the worse
    answer. **Reporting the loss is the half that was missing** — the canonical reaction came back
    looking complete, so a later "how many reactions used three components" query over the stored
    form is quietly wrong, and no version bump repairs it because the input was never recorded as
    partial. Chemclaw3 states the rule as `D-2026-08-08-a-partial-answer-must-say-so`.
    """
    written = [
        token
        for slot in request.reaction_smiles.split(">")
        for token in slot.split(".")
        if token.strip()
    ]
    seen: list[str] = []
    for token in [*written, *request.species]:
        if species.canonical_smiles(token) is None and token not in seen:
            seen.append(token)
    return seen


def _name(reactions: list[NamingRequest]) -> list[ReactionNaming]:
    """Classify each reaction; a miss is a result with null fields, not an omission."""
    stamp = _version().version
    answers = []
    for request in reactions:
        if _canonical_reaction(request.reaction_smiles) is None:
            continue
        found = naming.name(request.reaction_smiles)
        answers.append(
            ReactionNaming(
                id=request.id,
                version=stamp,
                named_reaction=found.named_reaction,
                reaction_class=found.reaction_class,
                method=found.method,
            )
        )
    return answers


def _all_species(reaction_smiles: str) -> list[str]:
    """Every species the reaction names, left to right — the default for the one-shot tool."""
    return [
        token for slot in reaction_smiles.split(">") for token in slot.split(".") if token.strip()
    ]


def _canonical_reaction(reaction_smiles: str) -> str | None:
    """The reaction re-written from its canonical parts, or `None` if it is not a reaction.

    Species-wise rather than through RDKit's reaction parser, because the record form routinely
    carries things a reaction parser rejects — an unbalanced extract, a bare ion, a species written
    with no atoms to map — and this server's job is to label those, not to refuse them.
    """
    parts = reaction_smiles.split(">")
    if len(parts) != 3:
        return None
    rewritten = []
    for slot in parts:
        canonical = [
            written
            for token in slot.split(".")
            if token.strip() and (written := species.canonical_smiles(token.strip())) is not None
        ]
        rewritten.append(".".join(canonical))
    return ">".join(rewritten) if rewritten[2] else None
