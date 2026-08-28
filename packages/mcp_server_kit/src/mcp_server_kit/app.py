"""`connector_app()` — wrap a `FastMCP` capability as the FastAPI app Chemclaw3 dials.

Every server in this repository is the same shape: `/healthz` for the startup probe, `/metrics` for
the scrape, `/mcp` for the MCP streamable-HTTP transport. That shape is written once here so a
server's own `app.py` is three lines, and so the cross-cutting behaviours it needs cannot be
forgotten one server at a time.

It is also the one place the process's *observability* is established, for the same reason: a log
configuration, a build-info label and a per-tool counter added one server at a time are added to
some of them. Before this, the fleet had none of the three — `configure_logging()` here is what
gives every line a timestamp and a level, and `_instrument_tool_calls` is what gives `/metrics`
anything about a tool at all.

These behaviours are not obvious, and each is here because getting it wrong is quiet (no count:
the one that stood here said four while the list below had five, and the list has grown again
since):

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
  worded, caller-safe messages) passes through **redacted** — a pydantic `ValidationError`, a
  `UnicodeDecodeError` and a `JSONDecodeError` are all `ValueError`s, and a validation message
  routinely quotes the input that failed, which is where a mounted secret would land; everything
  else is replaced and logged — with an `error_id` in *both* halves, because a notice the model can
  quote and a traceback nobody can find it in are two records of one fault that cannot be joined.
- **The trace must be continued per tool call, for the same reason the caller must.** Chemclaw3
  sends `traceparent` on every call and this fleet dropped it, so a connector's work was an orphan
  trace rather than a span inside the turn. It is picked up from the same request the caller is,
  because the serving request is only reachable there — see `tracing.py` for why nothing here
  exports anything.
- **`configure_logging()` must force.** `FastMCP.__init__` calls `basicConfig` at import of the
  server's `tools.py`, so anything that does not pass `force=True` here silently loses to it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from collections.abc import AsyncIterator, Callable, Coroutine, Iterable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.lowlevel.server import request_ctx
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest
from starlette.responses import Response

from mcp_server_kit.auth import BearerAuthMiddleware, BodySizeLimit, CallerLogMiddleware
from mcp_server_kit.datasets import Dataset
from mcp_server_kit.identity import (
    HEADER_ACTOR,
    HEADER_CORRELATION,
    HEADER_SESSION,
    bind_caller,
    reset_caller,
)
from mcp_server_kit.logging import configure_logging, redact_secrets, register_secret_env
from mcp_server_kit.metrics import BUILD_INFO, READY, TOOL_CALLS, TOOL_DURATION, UNKNOWN_TOOL
from mcp_server_kit.tracing import tool_call_span

logger = logging.getLogger(__name__)

# What a server passes to prove it can actually answer: a callable that loads whatever the first
# tool call would have loaded, and returns the corpora it verified. See `connector_app`.
Readiness = Callable[[], Iterable[Dataset]]

# One JSON-RPC call carrying chemistry-sized arguments. Far below a web front door's cap, because
# nothing legitimate on this surface is a file upload.
DEFAULT_MAX_REQUEST_BYTES = 1_000_000


def _requested_tool(args: tuple[Any, ...], kwargs: dict[str, Any]) -> object:
    """The tool name this `call_tool` invocation asked for, however upstream was called.

    `ToolManager.call_tool(name, arguments, ...)` is called positionally by the MCP server and by
    name in tests, and this reads both rather than pinning one — the wrappers around it must not
    change behaviour with the call style.
    """
    return args[0] if args else kwargs.get("name", "")


def _served_tool_name(manager: Any, requested: object) -> str:
    """`requested` if this server actually serves it, else the `<unknown>` sentinel.

    **The clamp that keeps a metric label bounded.** The name in a `tools/call` is caller-supplied
    and reaches `ToolManager.call_tool` unvalidated — it raises `Unknown tool: <whatever>` for
    anything it does not have — so counting it verbatim mints one Prometheus series per string a
    confused model or a hostile caller sends, unbounded, in the process's memory. Measured in the
    audit that prompted this: a probe calling `nope` minted `tool="nope"`.

    `get_tool` is a lookup in the same dict `list_tools()` returns, so this is O(1) and always
    reflects the surface as it stands rather than a snapshot taken at wrap time.
    """
    if isinstance(requested, str) and manager.get_tool(requested) is not None:
        return requested
    return UNKNOWN_TOOL


def _is_caller_safe(exc: ToolError) -> bool:
    """Whether this `ToolError` is a refusal the model may read, rather than a fault to hide.

    One definition, read by both `_sanitize_tool_errors` (which decides what the model is told) and
    `_instrument_tool_calls` (which decides whether the call counts as `refused` or `failed`). Two
    spellings of the same discriminator is how an operator's dashboard comes to disagree with what
    the agent was actually told.

    A deliberately worded domain message is a `ValueError` (pydantic's `ValidationError` is one);
    upstream folds a failing tool body into `ToolError(...) from e`, so a fault always arrives
    chained, and raises a bare `ToolError(f"Unknown tool: {name}")` for a name it does not have —
    a caller input error, and the only unchained one on this path.
    """
    return exc.__cause__ is None or isinstance(exc.__cause__, ValueError)


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


def _continue_trace_per_tool_call(server: FastMCP, *, name: str) -> None:
    """Open a span for each tool call, parented on the `traceparent` that call's request carried.

    Per tool call rather than in ASGI middleware, for exactly the reason
    `_bind_caller_per_tool_call` is: the tool body runs in the session manager's task, so a
    context attached in middleware is the *handshake's*. One MCP session carries many calls, and
    a span parented on the handshake's trace would put every subsequent call inside whichever turn
    happened to open the connection.

    Inert unless a deployment enables it, and it never exports anything itself — `tracing.py` holds
    that argument. With no request context (a direct call in a test) there is nothing to continue,
    so the tool runs unchanged.
    """
    manager = getattr(server, "_tool_manager", None)
    if manager is None:  # pragma: no cover - see `_bind_caller_per_tool_call`
        return
    wrapped = manager.call_tool

    async def call_tool(*args: Any, **kwargs: Any) -> Any:
        headers = getattr(getattr(request_ctx.get(None), "request", None), "headers", None)
        if headers is None:
            return await wrapped(*args, **kwargs)
        tool = str(args[0]) if args else str(kwargs.get("name", ""))
        with tool_call_span(headers, server=name, tool=tool):
            return await wrapped(*args, **kwargs)

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
            if _is_caller_safe(exc):
                # A caller-safe message still passes through `redact_secrets`. The family is wide —
                # a pydantic `ValidationError`, a `UnicodeDecodeError` and a `JSONDecodeError` are
                # all `ValueError`s — and a validation message routinely quotes the input that
                # failed, which is exactly where a mounted secret env value would land. The 503
                # readiness path already redacts; this branch did not, so a secret in a
                # caller-safe message reached the model verbatim. Re-raised unchanged when
                # redaction is a no-op, so the common case keeps its original traceback.
                redacted = redact_secrets(str(exc))
                if redacted == str(exc):
                    raise
                raise ToolError(redacted) from exc.__cause__
            # A short random token, minted here and put in *both* places: the operator's traceback
            # and the model's notice. Without it the two are unjoinable — the line said only which
            # server, so two concurrent faults in one second were indistinguishable and "the agent
            # said it broke" was not a thing anyone could grep for. A random hex is not an actor, a
            # session or a tool argument, so handing it to the model costs nothing.
            error_id = secrets.token_hex(4)
            tool = _served_tool_name(manager, _requested_tool(args, kwargs))
            logger.exception(
                "server %s: tool %s raised an unexpected exception (error id %s)",
                name,
                tool,
                error_id,
                extra={"error_id": error_id, "tool": tool},
            )
            raise ToolError(f"an internal error occurred (error id {error_id})") from exc.__cause__

    manager.call_tool = call_tool


def _instrument_tool_calls(server: FastMCP, *, name: str) -> None:
    """Count and time every tool call, at the one seam the whole fleet shares.

    `_tool_manager.call_tool` is where `_sanitize_tool_errors` and `_bind_caller_per_tool_call`
    already reach, so instrumenting it costs one more wrapper here and **no per-server change** —
    which is why the fleet had no per-tool signal at all: there was no other place to add one once
    without adding it seven times.

    `outcome` splits on `_is_caller_safe`, the same discriminator the sanitiser uses, and the split
    is the operationally important half of this metric. A rising `refused` on `props` means the
    model keeps asking for solvents the corpus does not carry — a prompt or a catalogue problem. A
    rising `failed` means the server is broken. Neither was visible before, and one counter for
    both would have made them indistinguishable.

    Applied *outside* the sanitiser so `outcome` describes what the caller was actually told, and
    outside the caller binding for the same reason the exposition carries no identity: this
    wrapper must never read who is asking.
    """
    manager = getattr(server, "_tool_manager", None)
    if manager is None:  # pragma: no cover - see `_bind_caller_per_tool_call`
        return
    wrapped = manager.call_tool

    async def call_tool(*args: Any, **kwargs: Any) -> Any:
        tool = _served_tool_name(manager, _requested_tool(args, kwargs))
        started = time.perf_counter()
        outcome = "ok"
        try:
            return await wrapped(*args, **kwargs)
        except ToolError as exc:
            outcome = "refused" if _is_caller_safe(exc) else "failed"
            raise
        except BaseException:
            # Anything that is not a `ToolError` never reached the sanitiser's judgement, so it is
            # a fault by definition — including a cancellation, which on this server means a caller
            # that gave up mid-calculation and is exactly the event `servers/calc` needs to see.
            outcome = "failed"
            raise
        finally:
            TOOL_DURATION.labels(name, tool).observe(time.perf_counter() - started)
            TOOL_CALLS.labels(name, tool, outcome).inc()

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
    readiness: Readiness | None = None,
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
        readiness: Optional callable that loads whatever the first tool call would have loaded and
            returns the corpora it verified. `None` means this server has nothing to be unready
            about, and `/healthz` stays the constant 200 it always was.

    Returns:
        A FastAPI app exposing `GET /healthz`, `GET /metrics`, and the MCP endpoint at `/mcp`.
    """
    # First, and with `force=True` underneath it. `FastMCP.__init__` ran at import of the server's
    # `tools.py` and has already called `basicConfig`, so anything that does not force is a no-op
    # and the process keeps upstream's timestamp-free `"%(message)s"`. See `logging.py`.
    configure_logging()
    if token_env:
        # The credential this server checks on every request, into the redaction inventory — so it
        # cannot reach a log line through an exception message, an environment dump or a `repr`.
        register_secret_env(token_env)
    BUILD_INFO.labels(name, server_revision()).set(1)
    _stamp_revision(server)
    _sanitize_tool_errors(server, name=name)
    # Applied after the sanitizer so it wraps it: the caller is bound before anything else runs,
    # which is what lets a tool stamp a record with the turn that asked for it.
    _bind_caller_per_tool_call(server)
    # Outside the sanitiser, so the `outcome` it books is the one the caller was actually given
    # rather than the exception the tool body raised — those differ by design, and the difference
    # is exactly what `ok`/`refused`/`failed` is splitting on. See the function.
    _instrument_tool_calls(server, name=name)
    # Outermost of the four, so the span covers the whole call — the binding, the tool body, the
    # sanitiser's decision about what the caller is told, and the counter that records it. A span
    # that ended before the metric was booked would put the two records of one call fractionally
    # out of step, which is the sort of skew nobody debugs and everybody mistrusts.
    _continue_trace_per_tool_call(server, name=name)
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
    app.add_middleware(CallerLogMiddleware, server=name, revision=server_revision())
    # Added after the logger, so Starlette's add-order (most recent outermost) puts the credential
    # check outside it: an unauthenticated request is refused before anything logs or reads it.
    app.add_middleware(BearerAuthMiddleware, server=name, token_env=token_env)
    if max_request_bytes:
        app.add_middleware(BodySizeLimit, max_bytes=max_request_bytes)

    @app.get("/healthz")
    async def healthz() -> Response:
        """Liveness *and* readiness — and until `readiness` existed only the first was true.

        This route answering is evidence the session manager is running, because uvicorn accepts
        connections only once the lifespan above has completed. It was never evidence that the
        server could answer anything, and the difference is not theoretical: datasets load lazily
        here, so a `chem` pod whose corpus fails its checksum returned 200, passed the kubelet
        probe, took traffic and failed every tool call — while `load_dataset`'s own docstring says
        a bad corpus "fails at startup with the two hashes in the message", which is true only of
        the servers that happen to touch their corpus at import. Measured: `props` had its table
        loaded at import *by accident* (an incidental module-level `len(...)` in `tools.py`) and
        `chem` did not.

        So a server that has something to be unready about passes a `readiness` callable, and this
        route runs it: 503 with the reason on failure, and on success the corpora it verified, so
        an operator can confirm which version of which table a pod is serving without a shell.

        Cheap to call repeatedly by construction — every loader behind it is `lru_cache`d, so the
        checksum is paid once per process and the probe thereafter reads a dict.
        """
        payload: dict[str, object] = {
            "status": "ok",
            "server": name,
            "revision": server_revision(),
        }
        if readiness is None:
            READY.labels(name).set(1)
            return JSONResponse(payload)
        try:
            # Off the event loop: a readiness check reads and hashes a corpus, and on `calc` it can
            # reach a subprocess. Blocking here would stall every in-flight SSE stream in the pod
            # for the duration of a probe — the same trap `calc`'s `on_start` hoist exists for.
            verified = list(await asyncio.to_thread(readiness))
        except Exception as exc:
            READY.labels(name).set(0)
            logger.exception("server %s is not ready: %s", name, exc)
            # `/healthz` carries no bearer check — a kubelet probe has no identity — so this reason
            # reaches anything that can open a socket to the pod, unauthenticated. The log line
            # above passes through `SecretRedactingFilter`; this body does not go through logging
            # at all, so it needs the same scrub applied directly. Measured against exactly this
            # gap: `_sanitize_tool_errors` replaces a tool fault's text before the model ever sees
            # it, and this was the one other place a raw exception reached a caller verbatim — the
            # one with no credential guarding it.
            return JSONResponse(
                {**payload, "status": "unready", "reason": redact_secrets(str(exc))},
                status_code=503,
            )
        READY.labels(name).set(1)
        payload["datasets"] = [f"{corpus.name}@{corpus.version}" for corpus in verified]
        return JSONResponse(payload)

    @app.get("/metrics")
    async def metrics() -> Response:
        """Prometheus exposition. Unauthenticated, so what it may carry is bounded.

        Not "counts only", which is what this used to say and was never true: the default registry
        publishes `python_info` and the `process_*` collectors, and an operator wants them. It is
        no longer *only* those either — `mcp_server_kit.metrics` puts this fleet's own per-tool
        counters and latencies here, which is the whole reason an operator can now answer "which
        tool is slow" and "which tool is failing".

        What the endpoint may **never** carry, because it is served without a credential, is
        anything about a *caller*: an actor, a session, a correlation id, or a tool argument. A
        labelled counter such as `tool_calls_total{tool=..., actor=...}` would publish per-actor
        call volumes to anything that can reach the pod. A tool *name* is none of those four and is
        clamped to the served surface (`_served_tool_name`); a destination host is not clamped and
        is therefore not a label at all. `tests/test_connector_app.py` asserts both directions over
        the live exposition rather than leaving it to this docstring.
        """
        return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    # Mounted last: Starlette matches in definition order, so the two routes above win and
    # everything else — notably `/mcp` — falls through to the MCP transport.
    app.mount("/", mcp_app)
    return app
