"""Bearer authentication on `/mcp`, and the request-size cap that sits outside it.

Every network-reachable server declares `auth: {mode: bearer, token_env: ...}`, and this is the
serving side that checks it. Load-bearing rules:

- **Middleware, not a route dependency**: `/mcp` is mounted, and a mount bypasses dependencies.
- **Compared as bytes**: `compare_digest` on `str` raises on non-ASCII, turning a bad header into a
  500.
- **Fail closed**: a declared `token_env` that is unset or blank refuses every request.
- **Whitespace is stripped from both sides**, so a secret provisioned with a trailing newline
  still works; a secret and the same secret padded are therefore the same secret.

`OPEN_PATHS` stays unauthenticated for probes and scrapes, and must never expose anything about a
request or caller.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from hmac import compare_digest

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mcp_server_kit.identity import (
    HEADER_ACTOR,
    HEADER_CORRELATION,
    HEADER_DRY_RUN,
    HEADER_SESSION,
    bind_caller,
    reset_caller,
)
from mcp_server_kit.metrics import REQUESTS, UNAUTHENTICATED_REQUESTS

logger = logging.getLogger(__name__)

# The routes a kubelet probe or a Prometheus scrape reaches, with no identity to present. A refused
# liveness probe kills the container.
OPEN_PATHS = frozenset({"/healthz", "/livez", "/metrics"})

# What a request path may become as a metric label: a fixed set plus a sentinel, because the path
# is caller-supplied and would otherwise mint unbounded series.
_MCP_PATH = "/mcp"
_OTHER_PATH = "<other>"


def _is_open(path: str) -> bool:
    """Whether `path` is one of the unauthenticated probe routes.

    A trailing slash is the only normalisation: `//metrics`, `/HEALTHZ` or `/healthz/../mcp` are not
    these routes, and a prefix rule would open the MCP surface. `connector_app` serves both
    spellings.
    """
    return (path.rstrip("/") or "/") in OPEN_PATHS


def _labelled_path(path: str) -> str:
    """`path` folded onto the fixed route set this server actually has."""
    normalised = path.rstrip("/") or "/"
    if normalised in OPEN_PATHS or normalised == _MCP_PATH:
        return normalised
    return _OTHER_PATH


def _status_capturing_send(send: Send, record: Callable[[int], None]) -> Send:
    """`send`, with the status of the response start handed to `record` on the way past."""

    async def wrapped(message: Message) -> None:
        if message["type"] == "http.response.start":
            record(int(message["status"]))
        await send(message)

    return wrapped


class BearerAuthMiddleware:
    """Refuse anything outside `OPEN_PATHS` without the bearer token `token_env` names.

    Pure ASGI rather than `BaseHTTPMiddleware`, which adds a task group and stream per request.
    """

    def __init__(self, app: ASGIApp, *, server: str, token_env: str | None) -> None:
        """Bind the server's name (for the log line) and the env var holding its expected token.

        `token_env` is `None` for a loopback-only dev server, and every request passes. The variable
        is
        read per request, so a rotated secret is picked up without a restart.
        """
        self._app = app
        self._server = server
        self._token_env = token_env

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Check the credential, or pass straight through for the probe routes and `mode: none`."""
        if scope["type"] != "http" or self._token_env is None:
            await self._app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        if _is_open(path):
            await self._app(scope, receive, send)
            return
        # Stripped before the emptiness check: a whitespace-only variable is unset and must fail
        # closed.
        expected = os.environ.get(self._token_env, "").strip()
        scheme, _, offered = Headers(scope=scope).get("authorization", "").partition(" ")
        if (
            not expected
            or scheme.lower() != "bearer"
            or not compare_digest(
                offered.strip().encode("utf-8", "surrogateescape"),
                expected.encode("utf-8", "surrogateescape"),
            )
        ):
            UNAUTHENTICATED_REQUESTS.labels(self._server).inc()
            logger.warning(
                "server %s refused an unauthenticated request to %s",
                self._server,
                path,
            )
            await PlainTextResponse("unauthorized", status_code=401)(scope, receive, send)
            return
        await self._app(scope, receive, send)


class CallerLogMiddleware:
    """Bind the `X-Chemclaw-*` caller for the request's duration, then log what happened to it.

    Binding covers the HTTP path (`app._bind_caller_per_tool_call` covers tool bodies); neither is
    an
    access decision. The line is written in a `finally` after the downstream app completes — pure
    ASGI,
    so for an SSE tool call it is after the last body byte — and carries status, duration,
    correlation
    id and revision. It cannot name the tool (that is `chemclaw_mcp_tool_calls_total`'s job). Probe
    and
    scrape requests log at DEBUG.
    """

    def __init__(self, app: ASGIApp, *, server: str, revision: str) -> None:
        """Bind the server's name and build so one log line says which pod, and which image."""
        self._app = app
        self._server = server
        self._revision = revision

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Bind the caller, serve the request, and log its outcome exactly once."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        actor = headers.get(HEADER_ACTOR, "")
        session = headers.get(HEADER_SESSION, "")
        correlation = headers.get(HEADER_CORRELATION, "")
        path = str(scope.get("path", ""))
        tokens = bind_caller(actor, session, correlation)
        started = time.perf_counter()
        # Default for an unhandled exception: nothing below starts a response and uvicorn answers
        # 500.
        status = 500

        def record(code: int) -> None:
            nonlocal status
            status = code

        try:
            await self._app(scope, receive, _status_capturing_send(send, record))
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            # Logged before the reset, so `ContextFilter` stamps this line with the caller's ids
            # too.
            logger.log(
                logging.DEBUG if _is_open(path) else logging.INFO,
                "server %s request: path=%s status=%s duration_ms=%.1f actor=%s session=%s "
                "correlation=%s dry_run=%s revision=%s",
                self._server,
                path,
                status,
                elapsed_ms,
                actor or "-",
                session or "-",
                correlation or "-",
                headers.get(HEADER_DRY_RUN, "-"),
                self._revision,
            )
            reset_caller(tokens)


class RequestMetrics:
    """Count every HTTP request this process answers — from outside everything that can refuse one.

    Must be the outermost middleware, or the 401s and 413s that auth and the size cap answer
    directly
    are never counted. Pure ASGI so it reads the status actually sent. Labels: server, the path
    folded
    by `_labelled_path`, and status — never anything about the caller.
    """

    def __init__(self, app: ASGIApp, *, server: str) -> None:
        """Wrap `app`, booking one series per request it answers."""
        self._app = app
        self._server = server

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the request, recording the status of the response that reached the client."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        # Default for an unhandled exception: nothing below starts a response and uvicorn answers
        # 500.
        status = 500

        def record(code: int) -> None:
            nonlocal status
            status = code

        try:
            await self._app(scope, receive, _status_capturing_send(send, record))
        finally:
            REQUESTS.labels(
                self._server, _labelled_path(str(scope.get("path", ""))), str(status)
            ).inc()


class BodySizeLimit:
    """Refuse an oversized request body with 413 before any handler reads it.

    Pure ASGI so it can reject while streaming. A declared `content-length` over the cap is refused
    up
    front (a route that never reads its body would otherwise be served); the running total guards
    the
    chunked case. Going over signals the app with an `http.disconnect`, not an exception, because an
    exception can be caught into something else on the way; what the app says afterwards is dropped.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        """Wrap `app`, refusing bodies over `max_bytes`. A `max_bytes` of 0 disables the cap."""
        self._app = app
        self._max_bytes = max_bytes

    def _declared_length(self, scope: Scope) -> int | None:
        """The request's `content-length`, or `None` when it does not declare one."""
        for key, value in scope.get("headers", ()):
            if key == b"content-length":
                try:
                    return int(value)
                except ValueError:
                    return None
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Refuse a declared oversize outright, then count what actually arrives."""
        if scope["type"] != "http" or not self._max_bytes:
            await self._app(scope, receive, send)
            return
        declared = self._declared_length(scope)
        if declared is not None and declared > self._max_bytes:
            await PlainTextResponse("request body too large", status_code=413)(scope, receive, send)
            return
        seen = 0
        refused = False
        answered = False

        async def counting_receive() -> Message:
            """Deliver the body until it exceeds the cap, then report a client disconnect."""
            nonlocal seen, refused
            if refused:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self._max_bytes:
                    refused = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            """Drop whatever the app says about a request this middleware has already refused.

            Only until the app has started a response; cutting one off mid-stream would be a
            protocol error.
            """
            nonlocal answered
            if refused and not answered:
                return
            if message["type"] == "http.response.start":
                answered = True
            await send(message)

        try:
            await self._app(scope, counting_receive, guarded_send)
        except Exception:
            if not refused:
                raise
        if refused and not answered:
            await PlainTextResponse("request body too large", status_code=413)(scope, receive, send)
