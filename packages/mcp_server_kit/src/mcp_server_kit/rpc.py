"""`rpc_routes()` — a backend's tools as typed HTTP/JSON, beside MCP, through the same tool manager.

`POST /rpc/<tool>` takes the tool's `chemclaw_contracts` request model as the JSON body and answers
with the structured result an MCP `tools/call` returns. Invariants: a request is validated by the
contract model and then dispatched through the wrapped tool manager, so admission, sanitising, the
tool counters and the span are the MCP path's; auth, the size cap, request metrics and the caller
headers are middleware, so they cover `/rpc` as they cover `/mcp`; every refusal is bounded
(`refusal.py`) and every error is an `RpcError`.
"""

from __future__ import annotations

import asyncio
import json
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

from mcp_server_kit.limits import AtCapacityError, TimeBudgetExceeded
from mcp_server_kit.logging import redact_secrets
from mcp_server_kit.metrics import TOOL_CALLS
from mcp_server_kit.refusal import is_caller_safe, validation_message
from mcp_server_kit.tracing import tool_call_span
from mcp_server_kit.validation import INLINE_VALIDATION_MAX_BYTES

__all__ = ["rpc_routes"]

# How much of a caller-chosen operation name an error echoes back.
_ECHOED_OPERATION_CHARS = 64


def _error(status: int, kind: RpcErrorKind, message: str) -> Response:
    """The answer for `kind`, as the contract's `RpcError`."""
    return JSONResponse(RpcError(kind=kind, message=message).model_dump(), status_code=status)


def _tool_error(exc: ToolError, operation: str) -> Response:
    """The answer for a failed tool call, from what the sanitiser already decided to say.

    A full pod and a spent clock are told apart by their cause; any other caller-safe error is a
    refusal; anything else is a fault whose message is already the sanitiser's `error id` notice.
    Upstream's `Error executing tool <name>: ` lead is dropped so a marker such as
    `[calc-at-capacity]` opens `message`.
    """
    message = str(exc).removeprefix(f"Error executing tool {operation}: ")
    if isinstance(exc.__cause__, AtCapacityError):
        return _error(503, "at_capacity", message)
    if isinstance(exc.__cause__, TimeBudgetExceeded):
        return _error(422, "time_budget", message)
    if is_caller_safe(exc):
        return _error(400, "refused", message)
    return _error(500, "failed", message)


def _arguments(model: type[WireRequest], payload: object) -> dict[str, Any] | str:
    """The tool arguments `model` accepts from `payload`, or the bounded, redacted refusal text."""
    try:
        return model.model_validate(payload).wire()
    except ValidationError as exc:
        # Bounded before the redaction scan: a validation error quotes what the caller sent.
        return redact_secrets(validation_message(exc))


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
    """The router for `POST /rpc/<operation>`, one operation per `models` entry (a tool each).

    An operation outside `models` is a 404, so the served set is exactly `models`. Call it after the
    tool-call wrappers and `transport_security` are installed (`connector_app` does) and include the
    router before the `/` mount. Raises `ValueError` for an operation that is not a served tool
    with an output schema, or whose model is for another tool.
    """
    _check_served(server, models)
    guard = TransportSecurityMiddleware(server.settings.transport_security)
    manager = server._tool_manager
    router = APIRouter()

    def is_refusal(exc: BaseException) -> bool:
        """The counter's `refused`/`failed` split, as the span sees it."""
        return isinstance(exc, ToolError) and is_caller_safe(exc)

    def refuse(operation: str, message: str) -> Response:
        """A 400 `refused` for a body the tool manager never saw, counted as the tool's refusal."""
        TOOL_CALLS.labels(name, operation, "refused").inc()
        return _error(400, "refused", message)

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
        rejected = await guard.validate_request(request, is_post=True)
        if rejected is not None:
            return _error(rejected.status_code, "rejected", bytes(rejected.body).decode())
        raw = await request.body()
        try:
            payload = json.loads(raw)
        except (ValueError, RecursionError):
            return refuse(operation, "the request body is not JSON, or is nested too deeply")
        if len(raw) > INLINE_VALIDATION_MAX_BYTES:
            arguments = await asyncio.to_thread(_arguments, model, payload)
        else:
            arguments = _arguments(model, payload)
        if isinstance(arguments, str):
            return refuse(operation, arguments)
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
