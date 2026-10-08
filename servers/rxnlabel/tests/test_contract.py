"""The served `rxnlabel` surface is the wire `chemclaw_contracts.rxnlabel` types."""

from __future__ import annotations

import asyncio

import pytest
from chemclaw_contracts.rxnlabel import (
    RXNLABEL_REQUESTS,
    RXNLABEL_RESPONSES,
    NameReactionsRequest,
    NamingRequest,
    ReactionRequest,
    RepresentReactionsRequest,
)
from chemclaw_mcp_rxnlabel import tools
from mcp_server_kit.testing import assert_wire_contract
from pydantic import ValidationError


def test_the_served_surface_is_the_contract() -> None:
    """Every tool's input is exactly its request model, and its output satisfies its response."""
    served = asyncio.run(tools.server.list_tools())
    assert_wire_contract(served, RXNLABEL_REQUESTS, RXNLABEL_RESPONSES)


def test_a_dropped_answer_field_fails_the_check() -> None:
    """The check bites: an answer that lost `version` is not what the drain stamps labels with."""
    served = asyncio.run(tools.server.list_tools())
    drifted = []
    for tool in served:
        if tool.name == "name_reaction":
            schema = dict(tool.outputSchema or {})
            schema["properties"] = {k: v for k, v in schema["properties"].items() if k != "version"}
            schema["required"] = [name for name in schema["required"] if name != "version"]
            tool = tool.model_copy(update={"outputSchema": schema})
        drifted.append(tool)
    with pytest.raises(AssertionError, match="name_reaction output"):
        assert_wire_contract(drifted, RXNLABEL_REQUESTS, RXNLABEL_RESPONSES)


def test_a_batch_request_serialises_to_the_dicts_the_drain_sends() -> None:
    """`wire()` is the exact `{"reactions": [{id, reaction_smiles, species}]}` the client builds."""
    request = RepresentReactionsRequest(
        reactions=[ReactionRequest(id="r1", reaction_smiles="CC>>CC", species=["CC"])]
    )
    assert request.wire() == {
        "reactions": [{"id": "r1", "reaction_smiles": "CC>>CC", "species": ["CC"]}]
    }
    naming = NameReactionsRequest(reactions=[NamingRequest(id="r1", reaction_smiles="CC>>CC")])
    assert naming.wire() == {"reactions": [{"id": "r1", "reaction_smiles": "CC>>CC"}]}
    with pytest.raises(ValidationError):
        NameReactionsRequest(reactions=[], extra=1)  # type: ignore[call-arg]
