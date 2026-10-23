"""Refuse a NaN or an infinity in any tool argument, for every served tool, on both channels.

Nothing upstream refuses one, and there are two ways in:

- **A bare literal** (`NaN`, `Infinity`, `1e400`) is accepted by the transport's `json.loads` and
  rewritten to `null`, so an optional argument would silently read as "not given".
  `NonFiniteLiteralRefusal` checks the raw body before the transport.
- **A string** (`"nan"`, `"1e400"`) is coerced by pydantic's lax mode after the transport.
  `refuse_non_finite_arguments` adds a per-field validator to every tool's argument model.

A per-field `after` validator rather than `allow_inf_nan=False`, because model config does not
reach nested argument models. Not covered: floats in non-model containers beyond list/tuple/dict,
defaults (not validated), and tools registered after `connector_app` ran.

Reading the body whole is also where a body that stops arriving is caught: each chunk is awaited
for at most `BODY_READ_TIMEOUT_SECONDS`, then the request is answered 408 and its connection closed.
"""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any

from chemclaw_contracts.rpc import RPC_PREFIX, RpcError
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase
from mcp.types import INVALID_PARAMS, PARSE_ERROR
from pydantic import BaseModel, ValidationInfo, create_model, field_validator
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mcp_server_kit.limits import env_bound

#: The words every refusal carries, so a test can tell this refusal from any other on the same
#: argument without matching the whole sentence.
NON_FINITE_REFUSAL = "is not a finite number"

BODY_READ_TIMEOUT_SECONDS = env_bound(
    "MCP_BODY_READ_TIMEOUT_SECONDS",
    # The longest a body may pause between two chunks. A legitimate caller writes its body at once
    # (the NetworkPolicy admits Chemclaw3 only, in-cluster), and a stock MCP client abandons a
    # write after 30 s (`MCP_DEFAULT_TIMEOUT`); twice that, so a slow live client gives up first.
    default=60,
    # One second, because a pause is measured in whole seconds and the loop awaits each chunk: at
    # `0` no body could arrive at all.
    minimum=1,
    consequence="every request body would be refused with 408 before its first chunk could arrive",
)


def first_non_finite(value: object, path: str) -> str | None:
    """The path to the first NaN or infinity inside `value`, or `None` when there is none.

    Args:
        value: A validated argument — a float, a pydantic model, or a list, tuple or dict of them.
        path: How the caller spelled `value`, extended as the walk descends, so the refusal can
            name `peak_table[2].retention_time_min`.

    Returns:
        The path to the offending float, or `None`.
    """
    if isinstance(value, float):
        return None if math.isfinite(value) else path
    children: list[tuple[str, object]]
    if isinstance(value, BaseModel):
        children = [(f"{path}.{name}", getattr(value, name)) for name in type(value).model_fields]
    elif isinstance(value, list | tuple):
        children = [(f"{path}[{index}]", item) for index, item in enumerate(value)]
    elif isinstance(value, dict):
        children = [(f"{path}[{key!r}]", item) for key, item in value.items()]
    else:
        return None
    for child_path, child in children:
        found = first_non_finite(child, child_path)
        if found is not None:
            return found
    return None


def _refuse_non_finite(value: Any, info: ValidationInfo) -> Any:
    """Pass `value` through unchanged, or refuse it naming where the non-finite float sits.

    A `ValueError`, so pydantic folds it into a caller-safe `ValidationError`.
    """
    where = first_non_finite(value, info.field_name or "argument")
    if where is not None:
        raise ValueError(
            f"{where} {NON_FINITE_REFUSAL}. Every numeric argument must be finite: a NaN or an "
            "infinity is not a measurement, and passed into the arithmetic it comes back as an "
            "answer that looks like one."
        )
    return value


def finite_arguments(model: type[ArgModelBase]) -> type[ArgModelBase]:
    """`model` with every field refusing a non-finite float, under the same name and schema.

    A subclass, so fields, aliases and the advertised JSON schema are unchanged.
    """
    return create_model(
        model.__name__,
        __base__=model,
        __module__=model.__module__,
        __validators__={"refuse_non_finite": field_validator("*")(_refuse_non_finite)},
    )


def refuse_non_finite_arguments(server: FastMCP) -> None:
    """Install `finite_arguments` on every tool `server` serves.

    `FuncMetadata` reads `arg_model` on every call, so replacing it applies to every later call.
    """
    for tool in server._tool_manager.list_tools():
        tool.fn_metadata.arg_model = finite_arguments(tool.fn_metadata.arg_model)


def _non_finite_literal(body: bytes) -> tuple[str, object] | None:
    """The first non-finite number `body` spells as a bare JSON literal, and the request's id.

    Parsed with `json.loads` as the transport will; `parse_float` sees each literal as written,
    which catches `1e400`.

    Returns:
        `(literal, id)` for a refusal, or `None` for a clean body or one that is not JSON (which the
        transport refuses itself).

    Raises:
        RecursionError: the body is nested past the parser's depth; no transport can read it.
    """
    found: list[str] = []

    def constant(literal: str) -> None:
        found.append(literal)

    def number(literal: str) -> float:
        value = float(literal)
        if not math.isfinite(value):
            found.append(literal)
        return value

    try:
        parsed = json.loads(body, parse_constant=constant, parse_float=number)
    except ValueError:
        return None
    if not found:
        return None
    return found[0], parsed.get("id") if isinstance(parsed, dict) else None


class NonFiniteLiteralRefusal:
    """Refuse a body spelling `NaN`, `Infinity` or an overflowing number, before MCP reads it.

    Pure ASGI, installed inside the size cap and bearer check, so the buffered body is bounded and
    authenticated; a clean body is replayed byte for byte. A request with an id gets 200 with a
    JSON-RPC error carrying that id — an MCP client tears down the session on a 400 — and one
    without gets 400. A `/rpc/` request gets the contract's `RpcError` with 400. A body nested past
    the parser's depth is a 400 too, never a traceback. A body pausing past
    `BODY_READ_TIMEOUT_SECONDS` gets a plain-text 408 on either route, as the size cap's 413 is.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap `app`."""
        self._app = app

    @staticmethod
    async def _too_deep(scope: Scope, receive: Receive, send: Send) -> None:
        """Answer a body nested past the parser's depth: 400, in the vocabulary of its route."""
        message = "the request body is nested too deeply to be read"
        if str(scope.get("path", "")).startswith(f"{RPC_PREFIX}/"):
            content: dict[str, Any] = RpcError(kind="refused", message=message).model_dump()
        else:
            content = {
                "jsonrpc": "2.0",
                "id": "server-error",
                "error": {"code": PARSE_ERROR, "message": message},
            }
        await JSONResponse(content, status_code=400)(scope, receive, send)

    @staticmethod
    async def _too_slow(scope: Scope, receive: Receive, send: Send) -> None:
        """Answer a body that stopped arriving: 408, closing the connection it still holds."""
        await PlainTextResponse(
            "request body not received in time",
            status_code=408,
            headers={"Connection": "close"},
        )(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Read a POST body whole: 408 if it stalls, refused if it spells a non-finite number."""
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self._app(scope, receive, send)
            return
        chunks: list[bytes] = []
        ended: Message | None = None
        while True:
            try:
                # Per chunk, so a large body on a slow link is never cut while it is still moving.
                async with asyncio.timeout(BODY_READ_TIMEOUT_SECONDS):
                    message = await receive()
            except TimeoutError:
                await self._too_slow(scope, receive, send)
                return
            if message["type"] != "http.request":
                ended = message
                break
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        try:
            refused = None if ended is not None else _non_finite_literal(body)
        except RecursionError:
            await self._too_deep(scope, receive, send)
            return
        if refused is not None:
            literal, request_id = refused
            if str(scope.get("path", "")).startswith(f"{RPC_PREFIX}/"):
                await JSONResponse(
                    RpcError(
                        kind="refused",
                        message=(
                            f"the request spells {literal!r}, which {NON_FINITE_REFUSAL}. JSON has "
                            "no NaN or infinity. Send a finite number, or omit the argument."
                        ),
                    ).model_dump(),
                    status_code=400,
                )(scope, receive, send)
                return
            await JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id if request_id is not None else "server-error",
                    "error": {
                        "code": INVALID_PARAMS,
                        "message": (
                            f"the request spells {literal!r}, which {NON_FINITE_REFUSAL}. JSON has "
                            "no NaN or infinity, and this transport would otherwise have passed it "
                            "to the tool as null — for an optional argument, as though it had not "
                            "been given. Send a finite number, or omit the argument."
                        ),
                    },
                },
                status_code=400 if request_id is None else 200,
            )(scope, receive, send)
            return
        replayed = False

        async def replay() -> Message:
            """The body once, as one message; then whatever the client sends next (a disconnect)."""
            nonlocal replayed
            if not replayed:
                replayed = True
                if ended is not None:
                    return ended
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self._app(scope, replay, send)
