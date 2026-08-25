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

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from chemclaw_mcp_rxnlabel.engine import mapping, naming, roles, species, version

server = FastMCP("rxnlabel")

# One request may carry at most this many reactions. The bound exists because the request body is
# already capped in bytes by the transport, and a body of ten thousand one-line reactions is under
# that cap and is minutes of transformer time — a timeout the caller reads as an outage rather than
# as "ask for less".
MAX_BATCH = 500


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
            "the optional extras are installed, because it is queried by exact name."
        ),
    )


class ReactionRepresentation(BaseModel):
    """One reaction: its atom map where there is one, and every species it was asked about."""

    id: str
    reaction_smiles: str = Field(description="The canonical form of the reaction as given.")
    mapped_smiles: str | None = Field(
        default=None,
        description="Atom-mapped reaction SMILES, or null where no mapper is installed.",
    )
    species: list[SpeciesRepresentation] = Field(
        default_factory=list, description="Positional against the species list that was sent."
    )


class ReactionNaming(BaseModel):
    """One reaction's classification. Every field null where nothing matched."""

    id: str
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
async def represent_reactions(reactions: list[ReactionRequest]) -> RepresentBatch:
    """`represent_reaction` over a batch — the form a corpus-labelling drain should call.

    At most 500 reactions per request. A reaction that could not be represented is absent from
    `results` rather than present and empty, so a caller can record what it got and leave the rest.
    """
    _check_batch(reactions)
    return RepresentBatch(
        version=_version().version, results=await asyncio.to_thread(_represent, reactions)
    )


@server.tool()
async def name_reactions(reactions: list[NamingRequest]) -> NameBatch:
    """`name_reaction` over a batch — the form a corpus-labelling drain should call.

    At most 500 reactions per request. A reaction that could not be read is absent from `results`.
    """
    _check_batch(reactions)
    return NameBatch(version=_version().version, results=await asyncio.to_thread(_name, reactions))


def _check_batch(reactions: list[object]) -> None:
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
    answers = []
    for request in reactions:
        canonical = _canonical_reaction(request.reaction_smiles)
        if canonical is None:
            continue
        assigned = roles.assign(request.reaction_smiles, request.species)
        answers.append(
            ReactionRepresentation(
                id=request.id,
                reaction_smiles=canonical,
                mapped_smiles=mapping.map_reaction(request.reaction_smiles),
                species=[
                    SpeciesRepresentation(
                        smiles=species.canonical_smiles(raw) or raw,
                        role=role,
                        scaffold=species.scaffold(raw),
                        functional_groups=species.functional_groups(raw),
                    )
                    for raw, role in zip(request.species, assigned, strict=True)
                ],
            )
        )
    return answers


def _name(reactions: list[NamingRequest]) -> list[ReactionNaming]:
    """Classify each reaction; a miss is a result with null fields, not an omission."""
    answers = []
    for request in reactions:
        if _canonical_reaction(request.reaction_smiles) is None:
            continue
        found = naming.name(request.reaction_smiles)
        answers.append(
            ReactionNaming(
                id=request.id,
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
