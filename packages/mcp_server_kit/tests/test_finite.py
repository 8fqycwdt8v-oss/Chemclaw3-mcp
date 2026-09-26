"""A NaN or an infinity in a tool argument is refused, on both of the channels it arrives by.

The wire half is the one that had to be driven rather than argued: JSON has no NaN, so the natural
belief is that none can arrive. The bodies below are written by hand because an MCP client *cannot*
send one — pydantic serialises a NaN as `null` — and a test through the client would pass whether
or not the server refused anything. What arrives is decided by the server's parser, which accepts
the literals, and by its session loop, which then rewrites them to `null`: driven before
`NonFiniteLiteralRefusal`, `limit: NaN` reached an optional argument as `None` and the call
answered `isError: false`.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp_server_kit.app import connector_app
from mcp_server_kit.finite import NON_FINITE_REFUSAL, first_non_finite
from pydantic import BaseModel, ValidationError


class Point(BaseModel):
    """A nested argument model, validated under its own config rather than the tool's."""

    label: str
    value: float


def _probe_server() -> FastMCP:
    """One tool with a bare float, one with a nested model, one with a list of floats."""
    server = FastMCP("finite-probe")

    @server.tool()
    def scale(x: float) -> float:
        """Return twice `x` — which, for a NaN, is a NaN with nothing saying so."""
        return 2 * x

    @server.tool()
    def label_point(point: Point) -> str:
        """Name a nested point."""
        return f"{point.label}={point.value}"

    @server.tool()
    def total(values: list[float]) -> float:
        """Add up a list."""
        return sum(values)

    @server.tool()
    def within(value: float, limit: float | None = None) -> str:
        """Check `value` against a limit, if one is declared — `None` means "not declared"."""
        return "no limit declared" if limit is None else str(value <= limit)

    return server


@pytest.fixture(scope="module")
def probe() -> FastMCP:
    """The probe capability, wrapped by the real `connector_app` so the refusal is installed."""
    server = _probe_server()
    connector_app(server, name="finite-probe")
    return server


@pytest.fixture(scope="module")
def running(serving: Callable[..., Any]) -> Iterator[str]:
    """The same capability under uvicorn, for the one test about the bytes on the wire."""
    server = _probe_server()
    with serving(connector_app(server, name="finite-probe-wire"), require_ready=True) as base:
        yield base


def _refusal(exc: pytest.ExceptionInfo[ToolError]) -> str:
    """The validation message a refused call carries, asserting it is the caller-safe kind."""
    assert isinstance(exc.value.__cause__, ValidationError), (
        "a non-finite argument was not refused at validation, so it reached the tool body"
    )
    return str(exc.value)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, "nan", "Infinity", "1e400"])
async def test_a_non_finite_float_argument_is_refused_by_name(probe: FastMCP, bad: object) -> None:
    """Each spelling pydantic would otherwise coerce into a non-finite float, refused naming `x`.

    The strings are the half a bare float check would miss: pydantic's lax mode reads `"nan"` and
    `"Infinity"` as floats, and `"1e400"` overflows to `inf` without complaint.
    """
    with pytest.raises(ToolError) as refused:
        await probe._tool_manager.call_tool("scale", {"x": bad})
    assert f"x {NON_FINITE_REFUSAL}" in _refusal(refused)


async def test_a_nested_model_s_float_is_refused_with_its_path(probe: FastMCP) -> None:
    """`allow_inf_nan=False` on the argument model would not reach this: `Point` has its own."""
    with pytest.raises(ToolError) as refused:
        await probe._tool_manager.call_tool(
            "label_point", {"point": {"label": "a", "value": math.inf}}
        )
    assert f"point.value {NON_FINITE_REFUSAL}" in _refusal(refused)


async def test_a_float_inside_a_list_is_refused_with_its_index(probe: FastMCP) -> None:
    """The refusal names the element, so a caller with a long list can find it."""
    with pytest.raises(ToolError) as refused:
        await probe._tool_manager.call_tool("total", {"values": [1.0, 2.0, math.nan]})
    assert f"values[2] {NON_FINITE_REFUSAL}" in _refusal(refused)


async def test_a_finite_call_is_untouched(probe: FastMCP) -> None:
    """The replacement argument model must serve exactly what the original served."""
    assert await probe._tool_manager.call_tool("scale", {"x": 1.5}) == 3.0
    assert await probe._tool_manager.call_tool("total", {"values": [1.0, 2.0]}) == 3.0
    assert (
        await probe._tool_manager.call_tool("label_point", {"point": {"label": "a", "value": 1.0}})
        == "a=1.0"
    )


def test_the_advertised_schema_does_not_move() -> None:
    """A subclass carrying a validator, not a new surface: what the agent reads is unchanged."""
    before = {tool.name: tool.parameters for tool in _probe_server()._tool_manager.list_tools()}
    wrapped = _probe_server()
    connector_app(wrapped, name="finite-probe-schema")
    after = {tool.name: tool.parameters for tool in wrapped._tool_manager.list_tools()}
    assert after == before
    for tool in wrapped._tool_manager.list_tools():
        assert tool.fn_metadata.arg_model.model_json_schema() == before[tool.name]


def _answer(response: httpx.Response) -> dict[str, Any]:
    """The one JSON-RPC message in a streamable-HTTP answer, whether sent as SSE or as JSON."""
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        data = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
        return dict(json.loads(data[-1]))
    return dict(response.json())


@pytest.fixture
def session_client(running: str) -> Iterator[httpx.Client]:
    """An initialised MCP session driven by hand, so a body can say what a client never would."""
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    with httpx.Client(base_url=running, headers=headers, timeout=10.0) as client:
        opened = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "finite-test", "version": "0"},
                },
            },
        )
        assert opened.status_code == 200, opened.text
        session = opened.headers.get("mcp-session-id")
        if session:
            client.headers["mcp-session-id"] = session
        client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        yield client


def _call(client: httpx.Client, arguments: str) -> httpx.Response:
    """POST a `tools/call` of `within` whose arguments are the literal JSON text given."""
    body = (
        '{"jsonrpc": "2.0", "id": 7, "method": "tools/call", '
        f'"params": {{"name": "within", "arguments": {arguments}}}}}'
    )
    return client.post("/mcp", content=body.encode())


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e400"])
def test_a_literal_the_transport_would_turn_into_null_is_refused_on_the_wire(
    session_client: httpx.Client, literal: str
) -> None:
    """Into an *optional* argument, where `null` means "not declared" and nothing else would object.

    Answered 200 with a JSON-RPC error carrying the request's own id, so an SDK client hands it to
    the waiting call rather than tearing the session down on a 4xx.
    """
    response = _call(session_client, f'{{"value": 1.0, "limit": {literal}}}')
    assert response.status_code == 200, response.text
    answered = _answer(response)
    assert "result" not in answered, f"{literal} was served: {answered}"
    assert answered["id"] == 7
    assert NON_FINITE_REFUSAL in answered["error"]["message"]
    assert literal in answered["error"]["message"]


def test_a_clean_body_is_replayed_to_the_transport_unchanged(session_client: httpx.Client) -> None:
    """The refusal buffers the body; a clean one must still reach the tool byte for byte."""
    declared = _answer(_call(session_client, '{"value": 1.0, "limit": 2.5}'))
    assert declared["result"]["isError"] is False
    assert "True" in json.dumps(declared["result"])
    omitted = _answer(_call(session_client, '{"value": 1.0}'))
    assert "no limit declared" in json.dumps(omitted["result"])


def test_a_body_with_no_id_is_refused_with_a_400(session_client: httpx.Client) -> None:
    """Nobody is waiting on a notification, so there is no call to hand a JSON-RPC error to."""
    response = session_client.post(
        "/mcp", content=b'{"jsonrpc": "2.0", "method": "notifications/progress", "x": NaN}'
    )
    assert response.status_code == 400
    assert NON_FINITE_REFUSAL in response.text


def test_a_string_spelling_is_refused_by_the_argument_model_on_the_wire(
    session_client: httpx.Client,
) -> None:
    """`"NaN"` is ordinary JSON, so the body check passes it; the argument model is what refuses."""
    answered = _answer(_call(session_client, '{"value": 1.0, "limit": "NaN"}'))
    assert answered["result"]["isError"] is True
    assert f"limit {NON_FINITE_REFUSAL}" in json.dumps(answered["result"])


def test_the_walk_finds_the_first_non_finite_value_and_nothing_else() -> None:
    """The walk alone: models, lists, tuples and dicts, and the types it deliberately ignores."""
    assert first_non_finite(1.0, "a") is None
    assert first_non_finite(math.nan, "a") == "a"
    assert first_non_finite([1.0, (2.0, math.inf)], "a") == "a[1][1]"
    assert first_non_finite({"k": [math.nan]}, "a") == "a['k'][0]"
    assert first_non_finite(Point(label="p", value=-math.inf), "a") == "a.value"
    # An int cannot be non-finite, and a string is text rather than a number.
    assert first_non_finite(10**400, "a") is None
    assert first_non_finite("NaN", "a") is None
