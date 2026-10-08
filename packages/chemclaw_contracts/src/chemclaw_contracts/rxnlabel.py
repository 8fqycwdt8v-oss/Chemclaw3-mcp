"""The wire of the `rxnlabel` backend: five tools, their requests and the answers a drain reads.

`servers/rxnlabel` represents and names reactions; Chemclaw3's corpus-labelling drain stamps each
label with the `version` the answer carries. `servers/rxnlabel/tests/test_contract.py` fails when a
tool's served schema drifts from the models here.

Invariants: requests forbid unknown arguments; a batch answer holds only the reactions that were
represented, so a missing id means "not labelled this pass".
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from chemclaw_contracts._wire import WireRequest

__all__ = [
    "RXNLABEL_REQUESTS",
    "RXNLABEL_RESPONSES",
    "LabellerVersion",
    "LabellerVersionRequest",
    "NameBatch",
    "NameReactionRequest",
    "NameReactionsRequest",
    "NamingRequest",
    "ReactionNaming",
    "ReactionRepresentation",
    "ReactionRequest",
    "RepresentBatch",
    "RepresentReactionRequest",
    "RepresentReactionsRequest",
    "SpeciesRepresentation",
]


class ReactionRequest(BaseModel):
    """One reaction to represent: the id to answer under, the reaction, and the species to place."""

    id: str = Field(min_length=1)
    reaction_smiles: str = Field(min_length=1, description="`reactants>agents>products`.")
    species: list[str] = Field(default_factory=list, description="Positional against the answer.")


class NamingRequest(BaseModel):
    """One reaction to classify."""

    id: str = Field(min_length=1)
    reaction_smiles: str = Field(min_length=1, description="`reactants>agents>products`.")


class LabellerVersionRequest(WireRequest):
    """`labeller_version`: takes no arguments."""

    tool_name: ClassVar[str] = "labeller_version"


class RepresentReactionRequest(WireRequest):
    """`represent_reaction`."""

    tool_name: ClassVar[str] = "represent_reaction"
    reaction_smiles: str
    species: list[str] | None = None


class NameReactionRequest(WireRequest):
    """`name_reaction`."""

    tool_name: ClassVar[str] = "name_reaction"
    reaction_smiles: str


class RepresentReactionsRequest(WireRequest):
    """`represent_reactions`: the batch form a corpus drain calls."""

    tool_name: ClassVar[str] = "represent_reactions"
    reactions: list[ReactionRequest]


class NameReactionsRequest(WireRequest):
    """`name_reactions`: the batch form a corpus drain calls."""

    tool_name: ClassVar[str] = "name_reactions"
    reactions: list[NamingRequest]


class SpeciesRepresentation(BaseModel):
    """What one species is, and what it was doing."""

    smiles: str
    role: str = Field(
        description=(
            "starting-material, product, reagent, solvent, catalyst, ligand, base, additive or "
            "unknown (not found in the reaction)."
        )
    )
    scaffold: str | None = None
    functional_groups: list[str] = Field(default_factory=list)


class ReactionRepresentation(BaseModel):
    """One reaction's atom map and species, stamped with the labeller that produced it."""

    id: str
    version: str
    reaction_smiles: str
    mapped_smiles: str | None = None
    unreadable_species: list[str] = Field(default_factory=list)
    species: list[SpeciesRepresentation] = Field(default_factory=list)
    degraded: list[str] = Field(default_factory=list)


class ReactionNaming(BaseModel):
    """One reaction's classification; every field `None` where nothing matched."""

    id: str
    version: str
    named_reaction: str | None = None
    reaction_class: str | None = None
    rxno_id: str | None = None
    confidence: float | None = None
    method: str | None = None
    degraded: list[str] = Field(default_factory=list)


class RepresentBatch(BaseModel):
    """`represent_reactions`' answer."""

    version: str
    results: list[ReactionRepresentation] = Field(default_factory=list)


class NameBatch(BaseModel):
    """`name_reactions`' answer."""

    version: str
    results: list[ReactionNaming] = Field(default_factory=list)


class LabellerVersion(BaseModel):
    """What this deployment's labels are stamped with, and what went into it."""

    version: str
    components: dict[str, str]


RXNLABEL_REQUESTS: dict[str, type[WireRequest]] = {
    request.tool_name: request
    for request in (
        LabellerVersionRequest,
        RepresentReactionRequest,
        NameReactionRequest,
        RepresentReactionsRequest,
        NameReactionsRequest,
    )
}

#: The model each tool's answer is read through.
RXNLABEL_RESPONSES: dict[str, type[BaseModel]] = {
    "labeller_version": LabellerVersion,
    "represent_reaction": ReactionRepresentation,
    "name_reaction": ReactionNaming,
    "represent_reactions": RepresentBatch,
    "name_reactions": NameBatch,
}
