"""`rpc_routes()` — a backend's tools as typed HTTP/JSON, beside MCP, through the same tool manager.

`POST /rpc/<tool>` takes the tool's `chemclaw_contracts` request model as the JSON body and answers
with the tool's structured result, byte for byte what an MCP `tools/call` returns as
`structuredContent`. Load-bearing rules:

- **The tool manager is the one call path.** A request is validated by the contract model, then
  dispatched through `FastMCP`'s (wrapped) tool manager, so argument validation, admission, the
  non-finite refusal, error sanitising, `chemclaw_mcp_tool_calls_total` and the span are the MCP
  path's, not copies of it.
- **Identity is read per request** by `CallerLogMiddleware`, which binds the `X-Chemclaw-*` headers
  for the request task; a tool body reads the same `current_caller()` over either transport.
- **Auth, the size cap and request metrics are middleware**, not route code, so they cover `/rpc`
  exactly as they cover `/mcp`. Host, Origin and Content-Type are upstream's own check, run here.
- An error is an `RpcError`; a refusal is 400, a full pod 503, a fault 500 carrying the `error id`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from chemclaw_contracts.rpc import RPC_PREFIX, RpcError, RpcErrorKind, WireRequest
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecurityMiddleware
from pydantic import ValidationError
from starlette.responses import Response

from mcp_server_kit.limits import AtCapacityError
from mcp_server_kit.logging import redact_secrets
from mcp_server_kit.refusal import is_caller_safe
from mcp_server_kit.tracing import tool_call_span

__all__ = ["rpc_routes"]

logger = logging.getLogger(__name__)

# How much of a caller-chosen operation name an error echoes back.
_ECHOED_OPERATION_CHARS = 64


def _error(status: int, kind: RpcErrorKind, message: str) -> Response:
    """The answer for `kind`, as the contract's `RpcError`."""
    return JSONResponse(RpcError(kind=kind, message=message).model_dump(), status_code=status)


def _tool_error(exc: ToolError, operation: str) -> Response:
    """The answer for a failed tool call, from what the sanitiser already decided to say.

    A full pod is told apart by its cause (`AtCapacityError`, whose message the sanitiser passes);
    a caller-safe error is a refusal; anything else is a fault whose message is already the
    sanitiser's `error id` notice. Upstream's `Error executing tool <name>: ` lead is dropped, so a
    marker such as `[calc-at-capacity]` is at the head of `message`, as the contract says.
    """
    message = str(exc).removeprefix(f"Error executing tool {operation}: ")
    if isinstance(exc.__cause__, AtCapacityError):
        return _error(503, "at_capacity", message)
    if is_caller_safe(exc):
        return _error(400, "refused", message)
    return _error(500, "failed", message)


def _check_served(server: FastMCP, models: Mapping[str, type[WireRequest]]) -> None:
    """Refuse, at build, an operation the server could not answer.

    Each model must name its own tool, the server must serve it, and the tool must have an output
    schema (without one FastMCP returns no structured content for the route to send).
    """
    manager = server._tool_manager
    for operation, model in models.items():
        if model.tool_name != operation:
            raise ValueError(
                f"rpc operation {operation!r} is mapped to {model.__name__}, which is for "
                f"{model.tool_name!r}"
            )
        tool = manager.get_tool(operation)
        if tool is None:
            raise ValueError(f"rpc operation {operation!r} is not a tool this server serves")
        if tool.fn_metadata.output_schema is None:
            raise ValueError(f"rpc operation {operation!r} has no output schema to answer with")


def rpc_routes(server: FastMCP, *, name: str, models: Mapping[str, type[WireRequest]]) -> APIRouter:
    """The router serving `POST /rpc/<operation>` for each of `models`, a tool of `server` each.

    Call it after `server`'s tool-call wrappers and `transport_security` are installed
    (`connector_app` does), and include the router before the `/` mount.

    Args:
        server: The `FastMCP` whose tools answer.
        name: The server's name, for the span.
        models: Operation (tool) name to the contract's request model for it.

    Returns:
        A router with one route, `/rpc/{operation}`. An operation outside `models` is a 404, so
        the served set is exactly `models`.

    Raises:
        ValueError: An operation is not a served tool with an output schema, or its model is for
            another tool.
    """
    _check_served(server, models)
    guard = TransportSecurityMiddleware(server.settings.transport_security)
    manager = server._tool_manager
    router = APIRouter()

    def is_refusal(exc: BaseException) -> bool:
        """The counter's `refused`/`failed` split, as the span sees it."""
        return isinstance(exc, ToolError) and is_caller_safe(exc)

    @router.post(f"{RPC_PREFIX}/{{operation}}", include_in_schema=False)
    async def call(operation: str, request: Request) -> Response:
        model = models.get(operation)
        if model is None:
            return _error(
                404,
                "unknown_op",
                f"this server serves no operation {operation[:_ECHOED_OPERATION_CHARS]!r}; "
                f"it serves {sorted(models)}",
            )
        refused = await guard.validate_request(request, is_post=True)
        if refused is not None:
            return _error(refused.status_code, "rejected", bytes(refused.body).decode())
        try:
            payload = json.loads(await request.body())
        except ValueError:
            return _error(400, "refused", "the request body is not JSON")
        try:
            arguments = model.model_validate(payload).wire()
        except ValidationError as exc:
            # A validation error quotes its input, which is where a secret would land.
            return _error(400, "refused", redact_secrets(str(exc)))
        try:
            with tool_call_span(
                request.headers, server=name, tool=operation, is_refusal=is_refusal
            ):
                answer: Any = await manager.call_tool(
                    operation, arguments, context=None, convert_result=True
                )
        except ToolError as exc:
            return _tool_error(exc, operation)
        # `(unstructured, structured)`: the pair FastMCP gives the MCP transport.
        return JSONResponse(answer[1])

    return router
