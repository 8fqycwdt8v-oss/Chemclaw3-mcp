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
import os
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

    **Tools only.** `FastMCP.read_resource`/`get_prompt` are a different shape: the lowlevel server
    captures the bound method at `_setup_handlers()`, inside `FastMCP.__init__`, before this
    function ever sees the instance, so patching `server.read_resource` here would silently do
    nothing — unlike `manager.call_tool` below, which `FastMCP.call_tool`'s own body looks up
    afresh on every call. A resource or prompt handler calling `current_caller()` would read the
    handshake's identity rather than the request being served.
    `tests/test_fleet.py::test_no_server_registers_a_resource_or_a_prompt` is what keeps that gap
    from being silently exploited — no server may register either until this is extended.
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

    **A `ToolError` with no `__cause__` is upstream's own wording, and it passes too.** `Tool.run`
    folds a failing tool body into `ToolError(...) from e`, so a fault always arrives chained;
    `ToolManager.call_tool` raises a bare `ToolError(f"Unknown tool: {name}")` for a name it does
    not have, which is a caller input error and the only unchained one on this path. Treating it as
    a fault told a model that had guessed a stale tool name "an internal error occurred" — nothing
    it could act on, against this repository's own rule that a refusal names what was wrong — and
    logged a stack trace at ERROR for every such call, so an operator's alerting fired on a client
    mistake. Both halves of that upstream behaviour are pinned in
    `tests/test_connector_app.py`, because neither is a documented promise.

    **Tools only, for the same reason `_bind_caller_per_tool_call` gives.** `FastMCP.read_resource`
    does its own `except Exception as e: raise ResourceError(str(e))` around `resource.read()`, with
    no `ValueError` exemption and no redaction — a subprocess stderr line, a file path, a secret,
    reaching the model verbatim. `get_prompt` does the equivalent. Neither can be patched at the
    manager level the way `call_tool` is: the try/except is inside `FastMCP`'s own method body, not
    delegated to `_resource_manager`/`_prompt_manager`, and that body itself is already captured by
    the lowlevel dispatch table before this function runs. Guarded the same way — see
    `test_no_server_registers_a_resource_or_a_prompt`.
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


def server_revision() -> str:
    """The build this process is, or `"unknown"` — the one place that decides.

    Read from `MCP_SERVER_REVISION`, which the Containerfile sets from a build argument the release
    pipeline fills with the commit. `"unknown"` rather than a raise, because a server that refuses
    to start when it cannot name its own build is strictly worse than one that starts and says so —
    the same judgement `on_start` already makes.

    **The default is a real answer, and that is the trap this has to avoid.** Chemclaw3 shipped
    exactly this field (D-057) and it read `"unknown"` for eight months, because the field, the
    column and the test all existed and no build ever set the variable (REV-17, fixed by D-140).
    So `tests/test_fleet.py` asserts the *Containerfile* passes it through, not merely that this
    function reads it — a value nothing supplies is a provenance record that quietly says nothing.
    """
    return os.environ.get("MCP_SERVER_REVISION", "") or "unknown"


def _stamp_revision(server: FastMCP) -> None:
    """Put this build's revision where MCP already carries a server version.

    **The handshake is the seam, and it costs nothing.** `initialize()` returns `serverInfo`
    (`{name, version}`) on every session a client opens, so a caller learns which build answered
    without a second endpoint, a second round trip, or a field on every tool result. Left alone the
    version reports the *MCP SDK's* release — 1.29.0 — which is a true fact about the wrong thing
    and would read as provenance to anyone who did not check.

    `FastMCP.__init__` takes no `version`, though the lowlevel `Server` it wraps does, so this
    assigns through `_mcp_server`. That is a private attribute and therefore a real coupling: it is
    asserted directly in `tests/test_fleet.py`, which names this function, so an upstream rename
    turns a test red here rather than silently reverting every server to reporting the SDK version.
    """
    server._mcp_server.version = server_revision()


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
    _stamp_revision(server)
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
        app.add_middleware(BodySizeLimit, max_bytes=max_request_bytes)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness and readiness for Chemclaw3's startup probe.

        One route for both, honestly: uvicorn accepts connections only once the lifespan above has
        completed, so this route answering *is* the evidence that the session manager is running.
        A separate `/readyz` could only assert the same fact twice.
        """
        return {"status": "ok", "server": name, "revision": server_revision()}

    @app.get("/metrics")
    async def metrics() -> Response:
        """Prometheus exposition. Unauthenticated, so what it may carry is bounded.

        Not "counts only", which is what this used to say and was never true: the default registry
        publishes `python_info` and the `process_*` collectors, and an operator wants them. What
        the endpoint may **never** carry, because it is served without a credential, is anything
        about a request — a caller's actor or session, a correlation id, or a tool argument. A
        labelled counter such as `tool_calls_total{tool=..., actor=...}` would publish per-actor
        call volumes to anything that can reach the pod. `tests/test_connector_app.py` asserts that
        absence over the live exposition rather than leaving it to this docstring.
        """
        return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    # Mounted last: Starlette matches in definition order, so the two routes above win and
    # everything else — notably `/mcp` — falls through to the MCP transport.
    app.mount("/", mcp_app)
    return app
