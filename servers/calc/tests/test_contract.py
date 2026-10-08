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

#: Present on the contract's `Structure` only: the server recomputes it, so no request carries it.
OUTPUT_ONLY = ("structure_id",)


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
    assert_wire_contract(_served(), CALC_REQUESTS, CALC_RESPONSES, ignore=OUTPUT_ONLY)


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
        assert_wire_contract(
            _drifted(served, predict_pka=renamed), CALC_REQUESTS, CALC_RESPONSES, ignore=OUTPUT_ONLY
        )


def test_a_retyped_default_fails_the_check() -> None:
    """A changed default is a changed wire: callers that omit the argument get another answer."""
    served = _served()
    schema = next(tool for tool in served if tool.name == "scan_point").inputSchema
    properties = {**schema["properties"], "solvent": {**schema["properties"]["solvent"]}}
    properties["solvent"]["default"] = "water"
    with pytest.raises(AssertionError, match=r"scan_point input\.solvent"):
        assert_wire_contract(
            _drifted(served, scan_point={**schema, "properties": properties}),
            CALC_REQUESTS,
            CALC_RESPONSES,
            ignore=OUTPUT_ONLY,
        )


def test_a_tool_without_a_model_fails_the_check() -> None:
    """A tool added to the server and not to the contract is drift, not an extra."""
    narrowed: dict[str, type[BaseModel]] = {
        name: model for name, model in CALC_RESPONSES.items() if name != "predict_pka"
    }
    with pytest.raises(AssertionError, match="response models cover"):
        assert_wire_contract(_served(), CALC_REQUESTS, narrowed, ignore=OUTPUT_ONLY)


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
