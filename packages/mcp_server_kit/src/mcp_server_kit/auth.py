"""Bearer authentication on `/mcp`, and the request-size cap that sits outside it.

Chemclaw3's manifest refuses `auth: {mode: none}` on a non-loopback URL, so every server here that
is reachable across a network declares `auth: {mode: bearer, token_env: ...}` — and the *serving*
side has to actually check it. Chemclaw3 shipped a version where it did not: `BearerAuth` existed
only on the sending side, so a deployment mounted a secret, recorded the control as enabled, and
served every tool to anything that could reach the pod.

Three details are load-bearing, each because getting them wrong fails open or fails loudly in the
wrong place:

- **Middleware, not a route dependency.** `/mcp` is mounted, and a mount bypasses the enclosing
  app's dependencies entirely. `Depends(...)` would guard the two routes that need it least.
- **Compared as bytes.** `compare_digest` on `str` raises `TypeError` on non-ASCII, and Starlette
  decodes headers as latin-1 — so one non-ASCII byte in the header turns a security boundary into
  a 500 with a traceback that any remote party can produce at will.
- **Fail closed.** A declared `token_env` whose variable is missing or empty refuses every request.
  A misconfigured deployment must serve nothing, not everything.

`/healthz` and `/metrics` stay open: a kubelet probe and a Prometheus scrape happen independently
of any identity, and the exposition carries counts only.
"""

from __future__ import annotations

import logging
import os
from hmac import compare_digest

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mcp_server_kit.identity import (
    HEADER_ACTOR,
    HEADER_CORRELATION,
    HEADER_DRY_RUN,
    HEADER_SESSION,
    bind_caller,
    reset_caller,
)

logger = logging.getLogger(__name__)

OPEN_PATHS = frozenset({"/healthz", "/metrics"})


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Refuse anything outside `OPEN_PATHS` without the bearer token `token_env` names."""

    def __init__(self, app: ASGIApp, *, server: str, token_env: str | None) -> None:
        """Bind the server's name (for the log line) and the env var holding its expected token.

        `token_env` is `None` for a loopback-only dev server (`auth: {mode: none}` in the
        manifest), and every request passes through. It is read from the environment *per request*
        rather than at construction, so a rotated secret is picked up without a restart.
        """
        super().__init__(app)
        self._server = server
        self._token_env = token_env

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Check the credential, or pass straight through for the probe routes and `mode: none`."""
        if request.url.path in OPEN_PATHS or self._token_env is None:
            return await call_next(request)
        expected = os.environ.get(self._token_env, "")
        scheme, _, offered = request.headers.get("authorization", "").partition(" ")
        if (
            not expected
            or scheme.lower() != "bearer"
            or not compare_digest(
                offered.strip().encode("utf-8", "surrogateescape"),
                expected.encode("utf-8", "surrogateescape"),
            )
        ):
            logger.warning(
                "server %s refused an unauthenticated request to %s",
                self._server,
                request.url.path,
            )
            return PlainTextResponse("unauthorized", status_code=401)
        return await call_next(request)


class CallerLogMiddleware(BaseHTTPMiddleware):
    """Log the `X-Chemclaw-*` caller of every request and bind it for the request's duration.

    Binding here covers the HTTP path; `app._bind_caller_per_tool_call` covers the tool bodies,
    which run in a different task. Both are needed, and neither is an access decision.
    """

    def __init__(self, app: ASGIApp, *, server: str) -> None:
        """Bind the server's name so one log line identifies which capability was called."""
        super().__init__(app)
        self._server = server

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Log the caller, bind it, serve the request unchanged, and always reset."""
        actor = request.headers.get(HEADER_ACTOR, "")
        session = request.headers.get(HEADER_SESSION, "")
        logger.info(
            "server %s request: path=%s actor=%s session=%s dry_run=%s",
            self._server,
            request.url.path,
            actor or "-",
            session or "-",
            request.headers.get(HEADER_DRY_RUN, "-"),
        )
        tokens = bind_caller(actor, session, request.headers.get(HEADER_CORRELATION, ""))
        try:
            return await call_next(request)
        finally:
            reset_caller(tokens)


class BodySizeLimit:
    """Refuse an oversized request body with 413 before any handler reads it.

    Pure ASGI rather than `BaseHTTPMiddleware` because the point is to reject *while streaming*:
    a declared `content-length` is a claim, and a chunked upload has none at all, so the running
    total is what actually bounds memory. Every legitimate request to an MCP server is one
    JSON-RPC call whose arguments are chemistry-sized — a SMILES string, a list of solvent names —
    never a file, which is why the default here is far below a web front door's.

    **Both halves are needed, and the first is not an optimisation.** Counting alone bounds only
    what somebody *reads*: a route that ignores the body never pulls from the receive channel, so
    an oversized request to it was served 200 with the cap installed and silent (measured — this
    class shipped with the counter only, and the test for it passed for the wrong reason). The
    declared `content-length` is therefore refused up front, and the running total still guards
    the chunked case where no such declaration exists.
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

        async def counting_receive() -> Message:
            nonlocal seen, refused
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self._max_bytes:
                    refused = True
                    raise _BodyTooLarge
            return message

        try:
            await self._app(scope, counting_receive, send)
        except _BodyTooLarge:
            if refused:
                await PlainTextResponse("request body too large", status_code=413)(
                    scope, receive, send
                )
                return
            raise


class _BodyTooLarge(Exception):
    """Internal signal from the counting receive channel to `BodySizeLimit.__call__`."""
