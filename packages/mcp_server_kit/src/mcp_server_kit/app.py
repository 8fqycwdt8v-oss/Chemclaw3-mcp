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
  worded, caller-safe messages) passes through; everything else is replaced and logged — with an
  `error_id` in *both* halves, because a notice the model can quote and a traceback nobody can
  find it in are two records of one fault that cannot be joined.
- **The trace must be continued per tool call, for the same reason the caller must.** Chemclaw3
  sends `traceparent` on every call and this fleet dropped it, so a connector's work was an orphan
  trace rather than a span inside the turn. It is picked up from the same request the caller is,
  because the serving request is only reachable there — see `tracing.py` for why nothing here
  exports anything.
- **`configure_logging()` must force, and must not run at import.** `FastMCP.__init__` calls
  `basicConfig` at import of the server's `tools.py`, so anything that does not pass `force=True`
  silently loses to it. But every server builds its app at *module scope*, so calling it from
  `connector_app` made reconfiguring the host process's root logger a side effect of an `import`:
  measured, importing `chemclaw_mcp_props.app` removed a handler the importer had installed, moved
  the root level from DEBUG to INFO, and added two filters to `logging.lastResort` permanently. It
  runs from the app's `lifespan` instead, which is still after upstream's `basicConfig` and after
  uvicorn's own `dictConfig` (`Config.__init__` runs that before it imports the app), so the
  ordering `force=True` exists for is unchanged and importing a module no longer changes a process.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from collections.abc import AsyncIterator, Callable, Coroutine, Iterable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.lowlevel.server import request_ctx
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest
from starlette.responses import Response

from mcp_server_kit.auth import (
    BearerAuthMiddleware,
    BodySizeLimit,
    CallerLogMiddleware,
    RequestMetrics,
)
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

# How long a failed readiness check is believed before it is run again. Short enough that a pod
# which has recovered is readied within one kubelet probe interval of recovering, long enough that
# a fleet of probes and scrapes against a *failing* pod cannot re-run the check per request. See
# `connector_app`'s `/healthz`.
READINESS_FAILURE_TTL_SECONDS = 5.0

# Set on a `FastMCP` the first time `connector_app` wraps it, so a second call is refused rather
# than silently doubling every count. See `_claim_server`.
_WRAPPED_BY = "_mcp_server_kit_wrapped_as"


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

    **The tool name is clamped the way a metric label is**, through `_served_tool_name`. A span name
    and a span attribute leave the pod for a collector, and the name in a `tools/call` is
    caller-supplied — so counting it verbatim mints one operation name per string a confused model
    retries with, which is the same unbounded cardinality `/metrics` refuses, one hop further out.
    Nothing accumulates in this process, which is why the two halves of one rule diverged quietly.

    Inert unless a deployment enables it, and it never exports anything itself — `tracing.py` holds
    that argument. With no request context (a direct call in a test) there is nothing to continue,
    so the tool runs unchanged.

    **The two things a span may say about a call are the two the metric already says**, and this
    wrapper reads them from the same functions the counter does rather than restating either.
    `_served_tool_name` clamps the name, because a span name is as caller-supplied as a metric
    label and was recorded verbatim — measured, `mcp.tool/../../etc/passwd?a=b` and a 318-character
    name. `_is_caller_safe` decides whether a propagating exception is this server's fault, because
    every `ToolError` is an exception and a *refusal* was therefore an ERROR span while the counter
    for the same call booked `outcome="refused"`.
    """
    manager = getattr(server, "_tool_manager", None)
    if manager is None:  # pragma: no cover - see `_bind_caller_per_tool_call`
        return
    wrapped = manager.call_tool

    def is_refusal(exc: BaseException) -> bool:
        """The counter's `refused`/`failed` split, as a span sees it.

        Anything that is not a `ToolError` never reached the sanitiser's judgement and is a fault
        by definition — the same reading `_instrument_tool_calls` makes of the same exception.
        """
        return isinstance(exc, ToolError) and _is_caller_safe(exc)

    async def call_tool(*args: Any, **kwargs: Any) -> Any:
        headers = getattr(getattr(request_ctx.get(None), "request", None), "headers", None)
        if headers is None:
            return await wrapped(*args, **kwargs)
        tool = _served_tool_name(manager, _requested_tool(args, kwargs))
        with tool_call_span(headers, server=name, tool=tool, is_refusal=is_refusal):
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
                raise
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


def _claim_server(server: FastMCP, *, name: str) -> None:
    """Refuse to wrap one `FastMCP` twice, because wrapping is not idempotent.

    Every one of the four behaviours `connector_app` installs is a wrapper around
    `_tool_manager.call_tool` that captures the previous value. A second call therefore *stacks* a
    second set: measured, one `tools/call` against a twice-wrapped server booked
    `chemclaw_mcp_tool_calls_total 2.0`, timed itself twice, opened two nested spans and ran the
    sanitiser twice. Nothing about that is visible at the call site, and every number the pod
    publishes is then wrong by a factor nobody can see.

    A raise rather than a silent skip: two apps over one capability is a mistake in the caller
    either way, and returning a second app whose wrappers belong to the first would be a stranger
    thing to debug than an exception naming the problem. A server that genuinely wants two apps
    builds two `FastMCP`s — which is what every test in this repository already does.
    """
    claimed = getattr(server, _WRAPPED_BY, None)
    if claimed is not None:
        raise RuntimeError(
            f"connector_app() has already wrapped this FastMCP as {claimed!r}; wrapping it again "
            f"as {name!r} would stack a second set of tool-call wrappers, so every metric, span "
            "and log line for one call would be recorded twice. Build a second FastMCP instead."
        )
    setattr(server, _WRAPPED_BY, name)


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

    Raises:
        RuntimeError: `server` has already been wrapped by a previous `connector_app` call. This
            is not idempotent and cannot be — see `_claim_server`.
    """
    _claim_server(server, name=name)
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
    # sanitiser's decision about what the caller is told, and the counter that records it. That is
    # a duration argument and nothing more: the span and the counter agree about the *outcome*
    # because both read `_is_caller_safe`, not because of the order they run in. Ordering was the
    # wrong axis and this comment used to name it as the right one — a refusal was an ERROR span
    # against a `refused` counter no matter which wrapper was outside which.
    _continue_trace_per_tool_call(server, name=name)
    mcp_app = server.streamable_http_app()
    # A pool of exactly one, and its own rather than the default. `asyncio.to_thread` hands work to
    # the *default* executor — the same pool every `servers/calc` tool offloads a calculation into,
    # and the one `engine/admission.py`'s ceiling does not govern — so a readiness check that
    # blocks is a readiness check competing with the tool calls it is meant to be reporting on.
    # Threads are created lazily, so a server with no `readiness` pays nothing for this.
    readiness_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"{name}-readiness")
    readiness_lock = asyncio.Lock()
    readiness_failure: tuple[float, str] | None = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Configure logging, then run the MCP session manager — the mount does not run it.

        **Logging is configured here rather than in `connector_app` itself**, because every server
        calls `connector_app` at module scope: doing it there made tearing out the host process's
        root handlers, forcing its level to INFO and permanently filtering `logging.lastResort` a
        side effect of `import chemclaw_mcp_<name>.app`. Startup is the first moment this process
        is unambiguously the one being configured, and it is still late enough for the ordering
        `force=True` exists for — `FastMCP.__init__`'s `basicConfig` ran at import of `tools.py`,
        and uvicorn's own `dictConfig` runs in `Config.__init__`, before it imports the app.
        """
        configure_logging()
        try:
            async with server.session_manager.run():
                report = asyncio.create_task(on_start()) if on_start is not None else None
                try:
                    yield
                finally:
                    if report is not None:
                        report.cancel()
        finally:
            readiness_pool.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title=f"chemclaw-mcp-{name}", lifespan=lifespan)
    app.add_middleware(CallerLogMiddleware, server=name, revision=server_revision())
    # Added after the logger, so Starlette's add-order (most recent outermost) puts the credential
    # check outside it: an unauthenticated request is refused before anything logs or reads it.
    app.add_middleware(BearerAuthMiddleware, server=name, token_env=token_env)
    if max_request_bytes:
        app.add_middleware(BodySizeLimit, max_bytes=max_request_bytes)
    # Last, therefore outermost of everything: the request counter has to see the requests the
    # layers above refuse, and both of those refuse before anything below them runs. Booked from
    # inside the caller log — one layer in — it saw neither a 401 nor a 413. See `RequestMetrics`.
    app.add_middleware(RequestMetrics, server=name)

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

        **Cheap to call repeatedly only on the path that succeeds**, and this docstring used to
        claim it for both. Every loader behind a readiness check is `lru_cache`d, and `lru_cache`
        does not cache *exceptions* — so on the one path the route exists for, a failing corpus,
        every probe re-ran the whole check. Measured over ASGI, counting only threads the *server*
        creates: 40 concurrent probes from cold produced **40** full readiness invocations spread
        over **8** `asyncio_*` threads — the interpreter's default executor, which on `calc` is the
        same pool every tool offloads a calculation into and the one `engine/admission.py`'s
        ceiling does not govern. A pod that cannot answer would have been spending the CPU of the
        calls it could still answer on proving that it cannot.

        So the failure is memoised for `READINESS_FAILURE_TTL_SECONDS` and the check is
        single-flighted behind one lock, on a pool of one thread of its own: the same 40 probes
        now run the check **once**, on one thread named for the server. A recovered pod is still
        readied within a probe interval, because the memo is seconds rather than minutes.

        (An earlier draft of this paragraph said "peaked at 47 threads against a baseline of 2".
        That number was measured with a 40-thread HTTP client and so was mostly the *harness*;
        the figures above count `asyncio_*` and the named pool only. A wrong number in prose is
        the thing this repository keeps writing decisions about, so it is corrected rather than
        quietly dropped.)

        **The reason is redacted, because this route is in `OPEN_PATHS`.** The body of a 503 is
        served to anything that can reach the pod, with no credential, and `str(exc)` on a loader
        failure is exactly the text a path, a DSN or a token ends up in — measured: a 503 carrying
        `PGPASSWORD=hunter2 token=Bearer abc123...` while the log line for the same exception was
        correctly scrubbed. `redact_secrets` is exported from `logging.py` for this, and #37 landed
        the same one-line conclusion on `main` from the other direction.

        **The memo is why that is two claims rather than one.** The reason string now lives in a
        second place, so "scrub what you return" and "scrub what you cache" can come apart:
        measured, with the return scrubbed and the memo holding `str(exc)`, the first probe's body
        was clean and every probe for the next five seconds leaked. The scrub therefore happens
        *once*, before the memo is written, and both bodies are the same string by construction.
        """
        nonlocal readiness_failure
        payload: dict[str, object] = {
            "status": "ok",
            "server": name,
            "revision": server_revision(),
        }
        if readiness is None:
            READY.labels(name).set(1)
            return JSONResponse(payload)

        def unready(reason: str) -> Response:
            """The 503, with the same redacted reason the memo holds."""
            READY.labels(name).set(0)
            return JSONResponse({**payload, "status": "unready", "reason": reason}, status_code=503)

        async with readiness_lock:
            if readiness_failure is not None and time.monotonic() < readiness_failure[0]:
                return unready(readiness_failure[1])
            try:
                # Off the event loop: a readiness check reads and hashes a corpus, and on `calc` it
                # can reach a subprocess. Blocking here would stall every in-flight SSE stream in
                # the pod for the duration of a probe — the same trap `calc`'s `on_start` hoist
                # exists for.
                verified = list(
                    await asyncio.get_running_loop().run_in_executor(readiness_pool, readiness)
                )
            except Exception as exc:
                # `/healthz` carries no bearer check — a kubelet probe has no identity — so this
                # reason reaches anything that can open a socket to the pod, unauthenticated. The
                # log line below passes through `SecretRedactingFilter`; this body does not go
                # through logging at all, so it needs the same scrub applied directly.
                # `_sanitize_tool_errors` replaces a tool fault's text before the model ever sees
                # it, and this was the one other place a raw exception reached a caller verbatim —
                # the one with no credential guarding it. (That is #37's finding and its wording;
                # it and this branch's memo were written against the same defect from two sides.)
                #
                # **Scrubbed once, here, and the memo holds the *redacted* string** — which is what
                # makes the two fixes compose rather than layer. A memo holding `str(exc)` would
                # have reintroduced the leak on every cached 503 while the freshly computed one
                # stayed clean, and nothing would have said the two disagreed.
                reason = redact_secrets(str(exc))
                readiness_failure = (time.monotonic() + READINESS_FAILURE_TTL_SECONDS, reason)
                logger.exception("server %s is not ready: %s", name, exc)
                return unready(reason)
            readiness_failure = None
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
