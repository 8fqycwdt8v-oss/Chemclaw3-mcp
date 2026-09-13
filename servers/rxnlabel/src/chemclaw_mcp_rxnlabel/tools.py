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
    DEFAULT_MAX_BATCH,
    DEFAULT_MAX_CONCURRENT_BATCHES,
    Admission,
)

server = FastMCP("rxnlabel")

# One request may carry at most this many reactions; `engine/admission.py` carries the measurement
# the default rests on. Read from the environment for the reason every other bound in this fleet
# is: a magic number is a bound nobody can loosen for a genuinely larger drain without editing
# code, and being readable is also what puts it in `tests/test_fleet.py`'s derived inventory of
# what a deployment can move.
MAX_BATCH = int(os.environ.get("CHEMCLAW_RXNLABEL_MAX_BATCH", str(DEFAULT_MAX_BATCH)))
if MAX_BATCH < 1:
    # `0` is the value an operator is most likely to try, because `MCP_MAX_SESSIONS=0` means "no
    # ceiling" one layer down — and here it meant the opposite and said nothing: every batch was
    # refused with "0 reactions in one request exceeds the batch limit of 0", on a pod that started
    # cleanly and passed its readiness probe. A bound whose whole job is to refuse cannot have an
    # "off", so this refuses at import instead, naming the variable a traceback from `int()` does
    # not. See `D-2026-09-12-a-bound-that-can-be-set-to-zero-has-to-say-what-zero-means`.
    raise ValueError(
        f"CHEMCLAW_RXNLABEL_MAX_BATCH={MAX_BATCH} would refuse every batch this server is asked "
        "for; a batch bound has no 'off' setting, so unset it for the default of "
        f"{DEFAULT_MAX_BATCH} or give it a positive number"
    )

# The pod's ceiling on concurrent labelling, built at import like the batch bound above; a test that
# needs a different ceiling replaces this attribute rather than the variable, because the number a
# gate enforces and the number it was built from must be the same number.
_MAX_CONCURRENT_BATCHES = int(
    os.environ.get("CHEMCLAW_RXNLABEL_MAX_CONCURRENT_BATCHES", str(DEFAULT_MAX_CONCURRENT_BATCHES))
)
if _MAX_CONCURRENT_BATCHES < 1:
    # `Admission` refuses this too, and its message names the ceiling rather than the variable that
    # set it — which leaves an operator with a CrashLoopBackOff and a number they have to guess the
    # source of. Same argument as the batch bound above.
    raise ValueError(
        f"CHEMCLAW_RXNLABEL_MAX_CONCURRENT_BATCHES={_MAX_CONCURRENT_BATCHES} would refuse every "
        "batch this server is asked for; an admission ceiling has no 'off' setting, so unset it "
        f"for the default of {DEFAULT_MAX_CONCURRENT_BATCHES} or give it a positive number"
    )
_admission = Admission(_MAX_CONCURRENT_BATCHES)

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
    degraded: list[str] = Field(
        default_factory=list,
        description=(
            "Components that were installed, ran on this reaction, and **failed** — "
            "`atom_mapper`, `reaction_namer`. Empty is the normal case. Non-empty means this "
            "answer is missing something a working pod would have produced, which is a different "
            "fact from a component this deployment never installed: that one shows up as "
            "`absent` in `version` and is not an error. Do not store a label with this non-empty "
            "as though it were complete; `version` also carries `failed` in the component's slot, "
            "so the row re-labels against a healthy pod."
        ),
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
    degraded: list[str] = Field(
        default_factory=list,
        description=(
            "`reaction_namer` when the classifier was installed, ran on this reaction and failed. "
            "Empty is the normal case — including the common one where the classifier ran and "
            "nothing matched, which is a real answer about the chemistry. Non-empty means the "
            "nulls above are a fault in this pod rather than a statement about the reaction, and "
            "`version` says `namer@failed` so the row re-labels."
        ),
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
        reaction_smiles: `reactants>agents>products`, agents kept. A string that is not a
            reaction at all is refused, naming what the form is; a reaction whose *species* could
            not all be read is answered, with those named in `unreadable_species`.
        species: The structures to classify, in your order; the answer is positional against it.
            Omit it to classify every species the reaction names, left to right.
    """
    request = ReactionRequest(
        id="1", reaction_smiles=reaction_smiles, species=species or _all_species(reaction_smiles)
    )
    return _the_one_answer(await asyncio.to_thread(_represent, [request]), reaction_smiles)


@server.tool()
@_admitted
async def name_reaction(reaction_smiles: str) -> ReactionNaming:
    """Classify one reaction into a named reaction and a reaction class.

    Answers "what reaction is this": a name a chemist recognises ("Buchwald-Hartwig amination",
    "Heck terminal vinyl") from 527 curated SMIRKS, plus the coarse class it belongs to.

    Every field is null when nothing matched, and that is a real answer rather than a failure —
    most of what a patent corpus contains has no name. It is also null when the optional classifier
    is not installed in this deployment; `labeller_version` is what tells the two apart, and what
    makes the rows re-label once it is. A string that is not a reaction at all — anything but
    `reactants>agents>products` — is refused rather than answered with nulls, because those two
    are different facts.
    """
    request = NamingRequest(id="1", reaction_smiles=reaction_smiles)
    return _the_one_answer(await asyncio.to_thread(_name, [request]), reaction_smiles)


@server.tool()
@_admitted
async def represent_reactions(reactions: list[ReactionRequest]) -> RepresentBatch:
    """`represent_reaction` over a batch — the form a corpus-labelling drain should call.

    There is a limit on how many reactions one request may carry; a larger batch is refused with a
    message naming the limit in force, so split it and re-send. A reaction that
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

    There is a limit on how many reactions one request may carry; a larger batch is refused with a
    message naming the limit in force, so split it and re-send. A reaction that
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


def _the_one_answer(answers: list[_T], reaction_smiles: str) -> _T:
    """The single-reaction form of a batch call that *drops* what it cannot read.

    The batch tools are deliberately lenient — a corpus-labelling drain wants what could be read
    and a list of what could not, rather than one bad row failing ten thousand good ones — so
    `_represent` and `_name` skip a reaction that is not `reactants>agents>products`. The
    single-reaction tools then took `[0]` of that list, and an unreadable input came back as
    `IndexError: list index out of range`: not a `ValueError`, so `connector_app` replaced it with
    an opaque `error_id` and the model was told a fault had occurred rather than that its input was
    malformed. Refuse in the caller's terms instead, which is what every other refusal in this
    fleet does.

    The offending string is quoted back because it is the caller's own, and truncated because a
    tool argument has no bound a message wants to inherit.
    """
    if answers:
        return answers[0]
    shown = reaction_smiles if len(reaction_smiles) <= 120 else reaction_smiles[:120] + "..."
    raise ValueError(
        f"this is not a reaction: {shown!r}. A reaction is written "
        "`reactants>agents>products` — three slots separated by two '>' characters, any of them "
        "empty — with each slot a '.'-separated list of SMILES. Nothing in this one could be read, "
        "so there is nothing to label."
    )


def _represent(reactions: list[ReactionRequest]) -> list[ReactionRepresentation]:
    """Represent each reaction, skipping the ones RDKit cannot read.

    **The stamp is per row, not per batch, and only because a row can be degraded.** A reaction the
    mapper raised on carries `mapper@failed` rather than the mapper's version, so it is stale
    against a healthy pod and re-labels; every other row carries the batch's own stamp, computed
    once. See `engine/version._component`.
    """
    stamp = _version().version
    answers = []
    for request in reactions:
        canonical = _canonical_reaction(request.reaction_smiles)
        if canonical is None:
            continue
        # **Mapped once.** `roles.assign` needs the map for the reactant-versus-reagent split and
        # the answer carries it as a field; deriving it in both places ran the transformer twice
        # per reaction, which is the cost `MAX_BATCH` was set against.
        attempt = mapping.map_reaction(request.reaction_smiles)
        mapped = attempt.mapped
        degraded = [mapping.COMPONENT] if attempt.failure is not None else []
        assigned = roles.assign(request.reaction_smiles, request.species, mapped)
        answers.append(
            ReactionRepresentation(
                id=request.id,
                version=version.labeller_version(degraded) if degraded else stamp,
                degraded=degraded,
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
    """Classify each reaction; a miss is a result with null fields, not an omission.

    A *failure* is a result with null fields too, and `degraded` plus a `namer@failed` stamp is what
    tells the two apart — see `_represent` for the same argument on the mapper.
    """
    stamp = _version().version
    answers = []
    for request in reactions:
        if _canonical_reaction(request.reaction_smiles) is None:
            continue
        found = naming.name(request.reaction_smiles)
        degraded = [naming.COMPONENT] if found.failure is not None else []
        answers.append(
            ReactionNaming(
                id=request.id,
                version=version.labeller_version(degraded) if degraded else stamp,
                degraded=degraded,
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
