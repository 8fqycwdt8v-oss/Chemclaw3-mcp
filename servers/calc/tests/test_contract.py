"""The served `calc` surface is the wire `chemclaw_contracts.calc` types, and drift fails here."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest
from chemclaw_contracts.calc import CALC_REQUESTS, CALC_RESPONSES, CalculationKeyRequest, Structure
from chemclaw_mcp_calc import tools
from mcp.types import Tool
from mcp_server_kit.testing import assert_wire_contract
from pydantic import BaseModel, ValidationError

#: On the contract's `Structure` for answers; the server recomputes it, so no request carries it.
REQUEST_IGNORES = ("structure_id",)

#: The content address Chemclaw3's cache keys on. FastMCP leaves a computed field out of
#: `outputSchema`, so these answers are checked on a real call instead (below).
UNLISTED = {"embed_structure": ("structure_id",), "combine_structures": ("structure_id",)}


def _check(served: Sequence[Tool]) -> None:
    """The wire check as this server runs it."""
    assert_wire_contract(
        served,
        CALC_REQUESTS,
        CALC_RESPONSES,
        ignore_in_requests=REQUEST_IGNORES,
        unlisted_in_answers=UNLISTED,
    )


def _served() -> list[Tool]:
    """The tools this server advertises, in process."""
    return asyncio.run(tools.server.list_tools())


def _drifted(served: Sequence[Tool], **replace: Any) -> list[Tool]:
    """`served` with the named tools' `inputSchema` replaced, to prove the check bites."""
    return [
        tool.model_copy(update={"inputSchema": replace[tool.name]})
        if tool.name in replace
        else tool
        for tool in served
    ]


def test_the_served_surface_is_the_contract() -> None:
    """Every tool's input is exactly its request model, and its output satisfies its response."""
    _check(_served())


def test_a_renamed_argument_fails_the_check() -> None:
    """The check bites: the served schema with `smiles` renamed is not the contract."""
    served = _served()
    schema = next(tool for tool in served if tool.name == "predict_pka").inputSchema
    renamed = {
        **schema,
        "properties": {"smile": schema["properties"]["smiles"]},
        "required": ["smile"],
    }
    with pytest.raises(AssertionError, match="predict_pka input"):
        _check(_drifted(served, predict_pka=renamed))


def test_a_retyped_default_fails_the_check() -> None:
    """A changed default is a changed wire: callers that omit the argument get another answer."""
    served = _served()
    schema = next(tool for tool in served if tool.name == "scan_point").inputSchema
    properties = {**schema["properties"], "solvent": {**schema["properties"]["solvent"]}}
    properties["solvent"]["default"] = "water"
    with pytest.raises(AssertionError, match=r"scan_point input\.solvent"):
        _check(_drifted(served, scan_point={**schema, "properties": properties}))


def test_a_tool_without_a_model_fails_the_check() -> None:
    """A tool added to the server and not to the contract is drift, not an extra."""
    narrowed: dict[str, type[BaseModel]] = {
        name: model for name, model in CALC_RESPONSES.items() if name != "predict_pka"
    }
    with pytest.raises(AssertionError, match="response models cover"):
        assert_wire_contract(_served(), CALC_REQUESTS, narrowed, ignore_in_requests=REQUEST_IGNORES)


def test_a_request_model_sends_only_what_the_caller_set() -> None:
    """`wire()` leaves defaults to the server, so `calculation_key` and the compute call agree."""
    structure = Structure(elements=[8, 1, 1], positions=[[0, 0, 0], [0.96, 0, 0], [-0.3, 0.9, 0]])
    request = CALC_REQUESTS["relax_structure"](structure=structure, solvent=None)  # type: ignore[call-arg]
    assert set(request.wire()) == {"structure", "solvent"}
    assert request.wire()["solvent"] is None


def test_an_argument_the_tool_does_not_take_is_refused() -> None:
    """The server refuses an unknown argument; so does the model that builds the call."""
    with pytest.raises(ValidationError):
        CALC_REQUESTS["predict_pka"](smiles="CCO", solvent="water")  # type: ignore[call-arg]
    key = CalculationKeyRequest(tool="predict_pka", arguments={"smiles": "CCO"})
    assert key.wire() == {"tool": "predict_pka", "arguments": {"smiles": "CCO"}}


def test_a_dropped_answer_field_fails_the_check() -> None:
    """`calculation_key` lists `structure_id`; a server that stops serving it is drift."""
    drifted = []
    for tool in _served():
        if tool.name == "calculation_key":
            schema = dict(tool.outputSchema or {})
            schema["properties"] = {
                k: v for k, v in schema["properties"].items() if k != "structure_id"
            }
            tool = tool.model_copy(update={"outputSchema": schema})
        drifted.append(tool)
    with pytest.raises(AssertionError, match=r"calculation_key output"):
        _check(drifted)


def test_the_unlisted_allowance_is_the_only_thing_hiding_the_structure_id() -> None:
    """Without it the check demands `structure_id` in the served schema, which FastMCP omits."""
    with pytest.raises(AssertionError, match=r"embed_structure output.*structure_id"):
        assert_wire_contract(
            _served(), CALC_REQUESTS, CALC_RESPONSES, ignore_in_requests=REQUEST_IGNORES
        )


@pytest.mark.parametrize("tool", ["embed_structure", "combine_structures"])
def test_a_structure_answer_carries_the_content_address_the_contract_names(tool: str) -> None:
    """The field the schema omits is on a real answer, and the contract model reads that answer."""
    _, water = asyncio.run(tools.server.call_tool("embed_structure", {"smiles": "O"}))
    arguments = {"smiles": "O"} if tool == "embed_structure" else {"first": water, "second": water}
    _, answer = asyncio.run(tools.server.call_tool(tool, arguments))
    parsed = Structure.model_validate(answer)
    assert parsed.structure_id is not None
    assert parsed.structure_id.startswith("st_")
