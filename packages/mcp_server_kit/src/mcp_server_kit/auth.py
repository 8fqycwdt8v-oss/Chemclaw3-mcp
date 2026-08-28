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
of any identity. The exposition is the default registry's — `python_info` and the `process_*`
collectors, which an operator wants — and never anything about a *request*: no caller identity, no
correlation id, no tool argument. That is the bound an unauthenticated endpoint has to keep, and it
is asserted over the live exposition in `tests/test_connector_app.py` rather than only stated here.
"""

from __future__ import annotations

import logging
import os
import time
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
from mcp_server_kit.metrics import REQUESTS, UNAUTHENTICATED_REQUESTS

logger = logging.getLogger(__name__)

OPEN_PATHS = frozenset({"/healthz", "/metrics"})

# What a request's path is allowed to become as a metric label. Three routes and a sentinel,
# because a path is caller-supplied: counting it verbatim would let anything that can reach the pod
# mint a series per URL it invents, which is the same unbounded-cardinality trap
# `app._served_tool_name` closes for a tool name.
_MCP_PATH = "/mcp"
_OTHER_PATH = "<other>"


def _is_open(path: str) -> bool:
    """Whether `path` is one of the two unauthenticated probe routes.

    A trailing slash is the only normalisation, and it is deliberately the only one: `OPEN_PATHS`
    was an exact-match set, so a kubelet probe configured as `path: /healthz/` got 401 forever and
    the pod never became ready — while the log said "refused an unauthenticated request", which
    reads as a credential problem and is not. Everything else stays an exact match: `//metrics`,
    `/HEALTHZ` and `/healthz/../mcp` are *not* these routes, and a prefix rule here would open the
    MCP surface to anything that could write a path starting with `/healthz`.
    """
    return (path.rstrip("/") or "/") in OPEN_PATHS


def _labelled_path(path: str) -> str:
    """`path` folded onto the fixed route set this server actually has."""
    normalised = path.rstrip("/") or "/"
    if normalised in OPEN_PATHS or normalised == _MCP_PATH:
        return normalised
    return _OTHER_PATH


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
        if _is_open(request.url.path) or self._token_env is None:
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
            UNAUTHENTICATED_REQUESTS.labels(self._server).inc()
            logger.warning(
                "server %s refused an unauthenticated request to %s",
                self._server,
                request.url.path,
            )
            return PlainTextResponse("unauthorized", status_code=401)
        return await call_next(request)


class CallerLogMiddleware(BaseHTTPMiddleware):
    """Bind the `X-Chemclaw-*` caller for the request's duration, then log what happened to it.

    Binding here covers the HTTP path; `app._bind_caller_per_tool_call` covers the tool bodies,
    which run in a different task. Both are needed, and neither is an access decision.

    **The line used to be written before `call_next` and that made it near-useless**, in three
    separate ways an operator meets on the same bad day:

    - A request that 500s, hangs, or is abandoned mid-stream logged *exactly* the same line as one
      that succeeded. There was no record anywhere that a request had ever finished, so "the pod
      stopped answering" and "the pod is answering fine" produced identical logs.
    - The **correlation id** — the single field that joins this fleet's lines to Chemclaw3's audit
      trail — was bound on every request and logged on none. Its only readers in the whole
      repository were `identity.py` and its own test.
    - Nothing said which build answered, so a log line could not be tied to an image.

    So it moves into a `finally`, and carries the status, the duration and the revision. The
    *counter* for the same event is not booked here — `RequestMetrics` does that from outside
    every middleware that can refuse a request, for the reason that class gives. What this line
    deliberately still cannot say is *which tool* was called — `path` is `/mcp` for every one of
    them, because the tool name is inside a JSON-RPC body this middleware must not parse. That
    question is answered by `chemclaw_mcp_tool_calls_total` instead, which is the right place for
    it: a per-tool rate is an aggregate, not a line.

    `/healthz` and `/metrics` drop to DEBUG. At an ordinary 30 s probe interval and a 30 s scrape
    that is on the order of 40,000 content-free lines a day across seven servers — plausibly the
    bulk of this fleet's log volume, describing the two requests nobody has ever needed to see one
    of. They are still emitted, at a level a deployment can turn back on with `MCP_LOG_LEVEL`.
    """

    def __init__(self, app: ASGIApp, *, server: str, revision: str) -> None:
        """Bind the server's name and build so one log line says which pod, and which image."""
        super().__init__(app)
        self._server = server
        self._revision = revision

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Bind the caller, serve the request, and log its outcome exactly once."""
        actor = request.headers.get(HEADER_ACTOR, "")
        session = request.headers.get(HEADER_SESSION, "")
        correlation = request.headers.get(HEADER_CORRELATION, "")
        path = request.url.path
        tokens = bind_caller(actor, session, correlation)
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            # Logged *before* the reset, deliberately. `ContextFilter` stamps every record from the
            # ambient caller, so resetting first left this one line — the request line — carrying
            # `[-/-]` where every line inside the request carried the ids. Measured against a
            # running server: the correlation id appeared in the message and not in the field a log
            # stack indexes, on the one record most worth joining on.
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
                request.headers.get(HEADER_DRY_RUN, "-"),
                self._revision,
            )
            reset_caller(tokens)


class RequestMetrics:
    """Count every HTTP request this process answers — from outside everything that can refuse one.

    **A counter whose help says "HTTP requests served, by route and response status" has to be the
    outermost thing in the stack, and this one was not.** It was booked in `CallerLogMiddleware`,
    and `connector_app` adds `BearerAuthMiddleware` and `BodySizeLimit` *after* it — so Starlette's
    add-order puts both outside the counter, and both short-circuit. Measured against a running
    server: three 401s and two 413s produced **zero** `chemclaw_mcp_requests_total` series between
    them, so `rate(chemclaw_mcp_requests_total{status=~"4.."})` — the expression an operator writes
    to see a fleet-wide credential or payload problem — was permanently 0. The one 4xx series that
    did appear was wrong: the chunked-oversize path books the *inner* app's 400, which the size cap
    then discards in favour of its own 413.

    Pure ASGI rather than `BaseHTTPMiddleware`, for the reason `BodySizeLimit` is: this has to sit
    outside a middleware that answers by writing directly to `send`, and it must observe the status
    that actually went out rather than a `Response` object some layer above may replace.

    The label set is unchanged and still bounded — the server's name, the path folded onto the
    fixed route set by `_labelled_path`, and the status. `/metrics` is unauthenticated, so nothing
    about the *caller* may join them; see `mcp_server_kit.metrics`.
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
        # The default is what an unhandled exception produces: nothing below ever starts a
        # response, and uvicorn answers 500 itself. Booking it is the point — a request that ended
        # in a fault is the one an operator most needs counted.
        status = 500

        async def counting_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self._app(scope, receive, counting_send)
        finally:
            REQUESTS.labels(
                self._server, _labelled_path(str(scope.get("path", ""))), str(status)
            ).inc()


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

    **The counter signals with a disconnect, not an exception, and that is the second correction
    this class has needed.** It used to raise a private exception through the receive channel. Two
    `BaseHTTPMiddleware` layers sit between this middleware and the app in every `connector_app`,
    each running the downstream app inside an `anyio` task group, so the signal arrived here
    wrapped in a nested `ExceptionGroup` that `except _BodyTooLarge` did not match: a chunked
    oversize body got **500 plus a per-request traceback**, and with no `BaseHTTPMiddleware` in
    between FastAPI's body parsing swallowed it into a 400 instead — the counter produced a 413 in
    no configuration at all, while its only test exercised the declared pre-check.

    An exception has to survive every `except` between here and the receive call, and there is no
    version of that this middleware controls. A disconnect is the ASGI protocol's own way to say
    "no more body is coming", every app already handles it, and it cannot be caught into something
    else. What the app then says is dropped, because the request has already been refused, and
    whatever it raises on the way out is a consequence of the refusal rather than a fault to
    report.
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

            Only until the app has started a response: an app that answered *before* the body went
            over the cap is mid-response, and cutting it off there would be a protocol error rather
            than a refusal.
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
