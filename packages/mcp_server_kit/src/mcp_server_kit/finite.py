"""Refuse a NaN or an infinity in any tool argument, for every served tool, on both channels.

**Nothing upstream refuses one, and there are two ways in, each invisible to the other's check.**
Measured against the pinned `mcp` 1.x and pydantic 2.13 over a real socket:

- **A bare literal is turned into `null` before any tool sees it.** JSON has no NaN, but the
  streamable-HTTP transport parses a body with `json.loads`, which accepts `NaN`, `Infinity`,
  `-Infinity` and overflows `1e400` to `inf`; the session loop then round-trips every request
  through `model_dump(mode="json")`, which writes a non-finite float as `null`. So a `float`
  argument refuses it as "not a valid number" — by luck — and a `float | None` argument **accepts
  it as `None`**, which in this fleet means "not declared": `system_suitability_report` given
  `minimum_resolution: NaN` skips the resolution check without a word about it. No
  argument model can refuse this, because what reaches it is a legitimate `None`. So
  `NonFiniteLiteralRefusal` reads the raw body, before the transport does.
- **A string is coerced into a non-finite float after the transport.** pydantic's lax mode reads
  `"nan"`, `"Infinity"` and `"1e400"` as floats, and those are ordinary JSON strings no body check
  can tell from text. So `refuse_non_finite_arguments` refuses on the argument model FastMCP
  validates every call against, after coercion.

Three servers had grown their own engine guards against this (`thermalsafety`, `suitability`,
`kinetics`), one argument at a time; neither channel above reaches an engine guard on an optional
argument, and twelve servers written argument by argument is twelve places to forget one. Both
halves are installed by `connector_app`, so a new server inherits them by being served.

**Why a validator walking the validated value, rather than `allow_inf_nan=False`.** That config key
is the idiomatic spelling and it does not reach far enough: pydantic applies a model's config to
that model's own fields, and a nested argument model — `calc`'s `Structure`, `suitability`'s
`PeakInput` — validates under its own. A per-field `after` validator sees the value once pydantic
has finished with it: coerced from a string, overflowed, nested models built. It is per *field*
rather than per model so a refusal is reported beside every other problem in the same call, rather
than only once everything else has already validated.

**What it does not cover, said rather than implied:** a float inside a container type other than a
pydantic model, a `list`, a `tuple` or a `dict` — no served argument uses one — and a *default*,
which pydantic does not validate and which a server author wrote on purpose. A tool registered after
`connector_app` has run is not reached by the argument half; every server registers at import of its
`tools.py`, and the fleet test drives the served surface through the app rather than trusting that.
"""

from __future__ import annotations

import json
import math
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase
from mcp.types import INVALID_PARAMS
from pydantic import BaseModel, ValidationInfo, create_model, field_validator
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: The words every refusal carries, so a test can tell this refusal from any other on the same
#: argument without matching the whole sentence.
NON_FINITE_REFUSAL = "is not a finite number"


def first_non_finite(value: object, path: str) -> str | None:
    """The path to the first NaN or infinity inside `value`, or `None` when there is none.

    Args:
        value: A validated argument — a float, a pydantic model, or a list, tuple or dict of them.
        path: How the caller spelled `value`, extended as the walk descends, so the refusal can
            name `peak_table[2].retention_time_min` rather than `peak_table`.

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

    A `ValueError` because that is what pydantic folds into a `ValidationError`, and a
    `ValidationError` is the family `connector_app` passes to the caller verbatim.
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

    A subclass rather than an edit: the fields, their aliases and the JSON schema FastMCP already
    advertised are inherited untouched, so the tool's surface does not move.
    """
    return create_model(
        model.__name__,
        __base__=model,
        __module__=model.__module__,
        __validators__={"refuse_non_finite": field_validator("*")(_refuse_non_finite)},
    )


def refuse_non_finite_arguments(server: FastMCP) -> None:
    """Install `finite_arguments` on every tool `server` serves.

    `FuncMetadata.call_fn_with_arg_validation` reads `self.arg_model` on every call, so replacing it
    here is what every subsequent `tools/call` validates against.
    """
    for tool in server._tool_manager.list_tools():
        tool.fn_metadata.arg_model = finite_arguments(tool.fn_metadata.arg_model)


def _non_finite_literal(body: bytes) -> tuple[str, object] | None:
    """The first non-finite number `body` spells as a bare JSON literal, and the request's id.

    Parsed the way the transport will parse it — `json.loads` — so what is refused here is exactly
    what would otherwise have been admitted and then rewritten to `null`. `parse_float` sees the
    literal as written, which is what catches `1e400`: it is valid JSON, and a float of it is `inf`.

    Returns:
        `(literal, id)` for a refusal — `id` so the error answers the request that was sent — or
        `None`: for a clean body, and for one that is not JSON at all, which the transport refuses
        in its own words.
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

    Pure ASGI, for the reason `auth.BodySizeLimit` gives, and installed *inside* that cap and the
    bearer check: the body it buffers is already bounded, and an unauthenticated one is never read.
    It replays the body it read, unchanged, so a clean request reaches the transport byte for byte.
    **A request is answered 200 with a JSON-RPC error carrying its own id**, the shape the transport
    uses for any tool call it refuses, rather than the 400 it uses for a body it cannot parse. The
    difference is what an MCP SDK client does with it: it delivers the first to the call that is
    waiting, and on the second `raise_for_status()` tears down the whole session. A body with no id
    — a notification, or something that is not JSON-RPC at all — has nobody waiting, and gets 400.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap `app`."""
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Read a POST body whole, refuse it if it spells a non-finite number, else replay it."""
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self._app(scope, receive, send)
            return
        chunks: list[bytes] = []
        ended: Message | None = None
        while True:
            message = await receive()
            if message["type"] != "http.request":
                ended = message
                break
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        refused = None if ended is not None else _non_finite_literal(body)
        if refused is not None:
            literal, request_id = refused
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
