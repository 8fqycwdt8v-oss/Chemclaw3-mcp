"""`connector_app()` — wrap a `FastMCP` capability as the FastAPI app Chemclaw3 dials.

Every server in this repository is the same shape: `/healthz` for the startup probe, `/metrics` for
the scrape, `/mcp` for the MCP streamable-HTTP transport. That shape is written once here so a
server's own `app.py` is three lines, and so the cross-cutting behaviours it needs cannot be
forgotten one server at a time.

Four of those behaviours are not obvious, and each is here because getting it wrong is quiet:

- **The parent app must run the MCP session manager.** `FastMCP.streamable_http_app()` returns a
  Starlette app whose *own* lifespan starts the session manager, and mounting an app does not run
  its lifespan. Miss this and the server accepts connections and then hangs on the first request —
  which looks like a network problem and is not.
- **Route order decides what `/mcp` reaches.** The MCP app is mounted at `/` and already serves
  `/mcp` itself, so `/healthz` and `/metrics` must be declared *before* the mount; Starlette matches
  in definition order.
- **The caller must be re-bound per tool call.** A tool body runs in the session manager's task,
  not the ASGI task, so the identity bound by middleware is the *handshake's*. Chemclaw3 measured
  the consequence: alice's handshake then bob's call had the tool reading alice.
- **A tool's unexpected exception must not reach the model verbatim.** `Tool.run` folds `str(e)`
  into the error result, so an unhandled internal error arrives at the agent carrying whatever the
  exception happened to mention. `ValueError` (the family this repository uses for deliberately
  worded, caller-safe messages) passes through; everything else is replaced and logged.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.lowlevel.server import request_ctx
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest
from starlette.responses import Response

from mcp_server_kit.auth import BearerAuthMiddleware, BodySizeLimit, CallerLogMiddleware
from mcp_server_kit.identity import (
    HEADER_ACTOR,
    HEADER_CORRELATION,
    HEADER_SESSION,
    bind_caller,
    reset_caller,
)

logger = logging.getLogger(__name__)

# One JSON-RPC call carrying chemistry-sized arguments. Far below a web front door's cap, because
# nothing legitimate on this surface is a file upload.
DEFAULT_MAX_REQUEST_BYTES = 1_000_000


def _bind_caller_per_tool_call(server: FastMCP) -> None:
    """Re-bind the caller from the request each tool call is serving.

    `request_ctx` is set per JSON-RPC message and carries the ASGI request, so the serving request
    is reachable from inside the tool body's task. With no request context — a direct call in a
    test — this falls through to whatever the middleware bound, which is the right behaviour.
    """
    manager = getattr(server, "_tool_manager", None)
    if manager is None:  # pragma: no cover - a future mcp release with a real middleware hook
        logger.warning("this mcp version exposes no tool manager; caller binding is HTTP-only")
        return
    wrapped = manager.call_tool

    async def call_tool(*args: Any, **kwargs: Any) -> Any:
        headers = getattr(getattr(request_ctx.get(None), "request", None), "headers", None)
        if headers is None:
            return await wrapped(*args, **kwargs)
        tokens = bind_caller(
            headers.get(HEADER_ACTOR, ""),
            headers.get(HEADER_SESSION, ""),
            headers.get(HEADER_CORRELATION, ""),
        )
        try:
            return await wrapped(*args, **kwargs)
        finally:
            reset_caller(tokens)

    manager.call_tool = call_tool


def _sanitize_tool_errors(server: FastMCP, *, name: str) -> None:
    """Replace an unexpected tool exception's text with a generic notice before a caller sees it.

    Not about *whether* the agent sees an error — it always does — only about what that error is
    allowed to say. A deliberately worded domain message is a `ValueError` (pydantic's
    `ValidationError` is one too); anything else is a bug or an infrastructure fault and is
    replaced, with the real exception logged so an operator can still find it.

    **A `ToolError` with no `__cause__` is the SDK's own wording, not a leak.** `Tool.run` wraps a
    tool's exception as `ToolError(...) from e`, so anything originating inside a tool body arrives
    with a cause; the only causeless one the SDK raises is `Unknown tool: <name>`. Replacing that
    told an agent that misspelled a tool — or called one the manifest advertises and the server no
    longer serves, which is exactly the drift this repository tests for — that an internal error had
    occurred, leaving it nothing to act on.
    """
    manager = getattr(server, "_tool_manager", None)
    if manager is None:  # pragma: no cover - see `_bind_caller_per_tool_call`
        return
    wrapped = manager.call_tool

    async def call_tool(*args: Any, **kwargs: Any) -> Any:
        try:
            return await wrapped(*args, **kwargs)
        except ToolError as exc:
            if exc.__cause__ is None or isinstance(exc.__cause__, ValueError):
                raise
            logger.exception("server %s: a tool raised an unexpected exception", name)
            raise ToolError("an internal error occurred") from exc.__cause__

    manager.call_tool = call_tool


def connector_app(
    server: FastMCP,
    *,
    name: str,
    token_env: str | None = None,
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
    on_start: Callable[[], Coroutine[Any, Any, None]] | None = None,
) -> FastAPI:
    """Build the FastAPI app that serves one capability over MCP streamable-HTTP.

    Args:
        server: The `FastMCP` instance holding this capability's tools. Everything it serves is
            reachable by anything that can open a socket to this process, so the served set is not
            a free surface — it must equal the `tools:` list in the server's `connector.yaml`, and
            each server's `tests/test_server.py` is what holds those two together.
        name: The server's name. Must match the directory, the package suffix, and the manifest's
            `name`, because Chemclaw3 addresses it by that one string.
        token_env: The environment variable holding the bearer token this server requires, or
            `None` for a loopback-only dev server. Chemclaw3's manifest validator refuses
            `auth: {mode: none}` on a non-loopback URL, so anything deployed sets this.
        max_request_bytes: Cap on one request body, refused with 413 before a handler reads it.
        on_start: Optional coroutine started once at startup, for a server that wants to report
            what it loaded. Started, never awaited — a server that refuses to start because it
            could not describe itself is strictly worse than one that starts.

    Returns:
        A FastAPI app exposing `GET /healthz`, `GET /metrics`, and the MCP endpoint at `/mcp`.
    """
    _sanitize_tool_errors(server, name=name)
    # Applied after the sanitizer so it wraps it: the caller is bound before anything else runs,
    # which is what lets a tool stamp a record with the turn that asked for it.
    _bind_caller_per_tool_call(server)
    mcp_app = server.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Run the MCP session manager for the app's lifetime — the mount does not run it."""
        async with server.session_manager.run():
            report = asyncio.create_task(on_start()) if on_start is not None else None
            try:
                yield
            finally:
                if report is not None:
                    report.cancel()

    app = FastAPI(title=f"chemclaw-mcp-{name}", lifespan=lifespan)
    app.add_middleware(CallerLogMiddleware, server=name)
    # Added after the logger, so Starlette's add-order (most recent outermost) puts the credential
    # check outside it: an unauthenticated request is refused before anything logs or reads it.
    app.add_middleware(BearerAuthMiddleware, server=name, token_env=token_env)
    if max_request_bytes:
        app.add_middleware(BodySizeLimit, server=name, max_bytes=max_request_bytes)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness and readiness for Chemclaw3's startup probe.

        One route for both, honestly: uvicorn accepts connections only once the lifespan above has
        completed, so this route answering *is* the evidence that the session manager is running.
        A separate `/readyz` could only assert the same fact twice.
        """
        return {"status": "ok", "server": name}

    @app.get("/metrics")
    async def metrics() -> Response:
        """Prometheus exposition for this process. Unauthenticated, and carries counts only."""
        return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    # Mounted last: Starlette matches in definition order, so the two routes above win and
    # everything else — notably `/mcp` — falls through to the MCP transport.
    app.mount("/", mcp_app)
    return app
