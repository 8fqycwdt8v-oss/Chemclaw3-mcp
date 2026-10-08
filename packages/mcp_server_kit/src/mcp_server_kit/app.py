"""`connector_app()` — wrap a `FastMCP` capability as the FastAPI app Chemclaw3 dials.

Every server has the same shape — `/healthz` (readiness), `/livez` (liveness), `/metrics`, and
`/mcp` (streamable HTTP) — written once here so the cross-cutting behaviours cannot be forgotten
one server at a time. Each is quiet when wrong:

- The parent app must run the MCP session manager; mounting an app does not run its lifespan.
- `/healthz` and `/metrics` are declared before the `/` mount; Starlette matches in order.
- The caller and the trace are re-bound per tool call, because a tool body runs in the session
  manager's task, not the request's.
- An unexpected tool exception never reaches the model verbatim (`_sanitize_tool_errors`).
- Validators, the default executor, session lifetime/ceiling, non-finite arguments and the
  DNS-rebinding allow-list are handled by `schema_cache`, `executor`, `sessions`, `finite` and
  `rebinding`.
- Logging, the validator cache and the executor are configured in the lifespan, not at import, so
  importing a server's `app` module changes nothing about the importing process.
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

from chemclaw_contracts import contract_version
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
from mcp_server_kit.degradation import (
    PERMANENT_CAUSES,
    classify,
    record,
    register_components,
)
from mcp_server_kit.executor import install_default_executor
from mcp_server_kit.finite import NonFiniteLiteralRefusal, refuse_non_finite_arguments
from mcp_server_kit.identity import (
    HEADER_ACTOR,
    HEADER_CORRELATION,
    HEADER_SESSION,
    bind_caller,
    reset_caller,
)
from mcp_server_kit.limits import effective_bounds
from mcp_server_kit.logging import configure_logging, redact_secrets, register_secret_env
from mcp_server_kit.metrics import BUILD_INFO, READY, TOOL_CALLS, TOOL_DURATION, UNKNOWN_TOOL
from mcp_server_kit.rebinding import apply_allowed_hosts
from mcp_server_kit.schema_cache import install_validator_cache
from mcp_server_kit.sessions import apply_session_ceiling, apply_session_idle_timeout
from mcp_server_kit.tracing import tool_call_span

logger = logging.getLogger(__name__)

# What a server passes to prove it can actually answer: a callable that loads whatever the first
# tool call would have loaded, and returns the corpora it verified. See `connector_app`.
Readiness = Callable[[], Iterable[Dataset]]

# One JSON-RPC call carrying chemistry-sized arguments. Far below a web front door's cap, because
# nothing legitimate on this surface is a file upload.
DEFAULT_MAX_REQUEST_BYTES = 1_000_000

# How long a failed readiness check is believed before it re-runs: short enough to ready a recovered
# pod within one probe interval, long enough that probes against a failing pod do not re-run it.
READINESS_FAILURE_TTL_SECONDS = 5.0

# The component a transient readiness failure is counted under; the failing part is in the log.
READINESS_COMPONENT = "readiness"
register_components(READINESS_COMPONENT)

# Set on a `FastMCP` the first time `connector_app` wraps it, so a second call is refused rather
# than silently doubling every count. See `_claim_server`.
_WRAPPED_BY = "_mcp_server_kit_wrapped_as"


def _requested_tool(args: tuple[Any, ...], kwargs: dict[str, Any]) -> object:
    """The tool name this `call_tool` invocation asked for, positional or keyword."""
    return args[0] if args else kwargs.get("name", "")


def _served_tool_name(manager: Any, requested: object) -> str:
    """`requested` if this server serves it, else the `<unknown>` sentinel.

    The name in a `tools/call` is caller-supplied, so a metric label or span name taken verbatim
    would mint unbounded series; this clamps it to the live served surface.
    """
    if isinstance(requested, str) and manager.get_tool(requested) is not None:
        return requested
    return UNKNOWN_TOOL


def _is_caller_safe(exc: ToolError) -> bool:
    """Whether this `ToolError` is a refusal the model may read, rather than a fault to hide.

    The one discriminator for both the sanitiser and the `refused`/`failed` metric split.
    Caller-safe: a chained `ValueError` (including pydantic's `ValidationError`), or upstream's
    unchained `Unknown tool` error; a failing tool body otherwise arrives chained to its real cause.
    """
    return exc.__cause__ is None or isinstance(exc.__cause__, ValueError)


def _bind_caller_per_tool_call(server: FastMCP) -> None:
    """Re-bind the caller from the request each tool call is serving.

    `request_ctx` carries the serving request into the tool body's task; with none (a direct call in
    a test) the middleware's binding stands. Tools only: resource and prompt handlers are captured
    before this runs, so no server may register either
    (`test_no_server_registers_a_resource_or_a_prompt`).
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

    Per tool call for the same reason as `_bind_caller_per_tool_call`. The span name is clamped by
    `_served_tool_name` and its error status follows `_is_caller_safe`, so span and metric agree.
    Inert unless a deployment enables tracing; nothing is exported from here (see `tracing.py`).
    """
    manager = getattr(server, "_tool_manager", None)
    if manager is None:  # pragma: no cover - see `_bind_caller_per_tool_call`
        return
    wrapped = manager.call_tool

    def is_refusal(exc: BaseException) -> bool:
        """The counter's `refused`/`failed` split, as a span sees it."""
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

    A caller-safe error (`_is_caller_safe`) passes, redacted; anything else is logged and replaced.
    Tools only: `FastMCP.read_resource`/`get_prompt` wrap errors inside their own bodies where this
    cannot reach, so no server may register either.
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
                # Even a caller-safe message is redacted: a validation error quotes its input, which
                # is where a secret would land. Re-raised unchanged when redaction is a no-op,
                # keeping the traceback.
                redacted = redact_secrets(str(exc))
                if redacted == str(exc):
                    raise
                raise ToolError(redacted) from exc.__cause__
            # One random token in both the operator's traceback and the model's notice, so the two
            # can be joined. It identifies nothing about the caller.
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

    `outcome` splits on `_is_caller_safe`: rising `refused` is a model/catalogue problem, rising
    `failed` a broken server. Applied outside the sanitiser so it books what the caller was told,
    and it never reads who is asking.
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
            # A non-`ToolError` never reached the sanitiser, so it is a fault — including a
            # cancellation.
            outcome = "failed"
            raise
        finally:
            TOOL_DURATION.labels(name, tool).observe(time.perf_counter() - started)
            TOOL_CALLS.labels(name, tool, outcome).inc()

    manager.call_tool = call_tool


def _claim_server(server: FastMCP, *, name: str) -> None:
    """Refuse to wrap one `FastMCP` twice, because wrapping is not idempotent.

    Every per-call behaviour wraps `_tool_manager.call_tool`, so a second wrap would double every
    count, span and sanitiser pass. Two apps need two `FastMCP`s.
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
    """The build this process is, or `"unknown"`.

    Read from `MCP_SERVER_REVISION`, set by the Containerfile from a build argument;
    `tests/test_fleet_images.py::test_the_image_can_name_the_build_it_is` asserts the
    Containerfile supplies it. Never raises: a server that
    cannot name its build still starts.
    """
    return os.environ.get("MCP_SERVER_REVISION", "") or "unknown"


def _stamp_revision(server: FastMCP) -> None:
    """Put this build's revision in the MCP handshake's `serverInfo.version`.

    Otherwise it reports the SDK's release. `FastMCP` takes no `version`, so this assigns through
    the private `_mcp_server`;
    `tests/test_fleet_images.py::test_the_revision_reaches_the_handshake_and_the_probe` pins that
    coupling.
    """
    server._mcp_server.version = server_revision()


def _declared_contract_version(name: str) -> str | None:
    """The `contract_version` the packaged manifest of `name` carries, `None` where it has none.

    Read from `chemclaw_contracts`, the one owner of the manifest, so `/healthz` cannot report a
    version the manifest does not declare. An app with no packaged manifest (a probe app in a test)
    reports none.
    """
    try:
        return contract_version(name)
    except KeyError:
        return None


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
        server: The `FastMCP` holding this capability's tools; its served set must equal the
            manifest's `tools:` list (`servers/*/tests/test_server.py`).
        name: The server's name, matching directory, package suffix and manifest `name`.
        token_env: Environment variable holding the required bearer token, or `None` for a
            loopback-only dev server.
        max_request_bytes: Cap on one request body, refused with 413 before a handler reads it.
        on_start: Optional coroutine started (never awaited) once at startup.
        readiness: Optional callable that loads what the first tool call would and returns the
            corpora it verified; `None` keeps `/healthz` a constant 200.

    Returns:
        A FastAPI app exposing `/healthz`, `/livez`, `/metrics` and `/mcp`.

    Raises:
        RuntimeError: `server` was already wrapped (`_claim_server`).
        ValueError: `MCP_ALLOWED_HOSTS` holds an unusable entry.
    """
    _claim_server(server, name=name)
    if token_env:
        # The credential this server checks on every request, into the redaction inventory — so it
        # cannot reach a log line through an exception message, an environment dump or a `repr`.
        register_secret_env(token_env)
    BUILD_INFO.labels(name, server_revision()).set(1)
    _stamp_revision(server)
    # Before any wrapper, because it is not one: it replaces each tool's argument model, so a NaN or
    # an infinity is a validation refusal like any other and every wrapper below books it as one.
    refuse_non_finite_arguments(server)
    _sanitize_tool_errors(server, name=name)
    # Applied after the sanitizer so it wraps it: the caller is bound before anything else runs,
    # which is what lets a tool stamp a record with the turn that asked for it.
    _bind_caller_per_tool_call(server)
    # Outside the sanitiser, so `outcome` is what the caller was told, not what the body raised.
    _instrument_tool_calls(server, name=name)
    # After the counter, so the span's duration covers the whole call.
    _continue_trace_per_tool_call(server, name=name)
    # Before `streamable_http_app()`, which reads it. Raises on an unusable entry, naming it.
    apply_allowed_hosts(server)
    mcp_app = server.streamable_http_app()
    # After `streamable_http_app()`, which builds the session manager this reaches into. Keeps a
    # session alive while a call runs, so a long calculation is not reaped as idle.
    apply_session_idle_timeout(server)
    # Installed last so the ceiling is outermost and a refused handshake never reaches the sweep.
    apply_session_ceiling(server, name=name)
    # A one-thread pool of its own: a blocking readiness check must not compete with tool calls in
    # the default executor. Threads are created lazily.
    readiness_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"{name}-readiness")
    readiness_lock = asyncio.Lock()
    # (expiry, redacted reason, cause). Transient verdicts are memoised too; the cause picks the
    # status.
    readiness_failure: tuple[float, str, str] | None = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Configure the process, then run the MCP session manager — the mount does not run it.

        Logging, the validator cache and the default executor are set up here, not in
        `connector_app`, so importing a server's `app` module has no process-wide side effects.
        `configure_logging()` here still runs after upstream's `basicConfig` and uvicorn's
        `dictConfig`; the executor needs a running loop.
        """
        configure_logging()
        install_validator_cache()
        tool_pool = install_default_executor(server=name)
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
            # After the session manager stopped. `wait=False` lets running calls finish without
            # blocking shutdown; `cancel_futures` would drop queued work silently.
            tool_pool.shutdown(wait=False)

    app = FastAPI(title=f"chemclaw-mcp-{name}", lifespan=lifespan)
    # First, therefore innermost: it buffers a body, so it runs only on one the size cap has bounded
    # and the bearer check has admitted, and its 400 is inside the caller log. See `finite.py`.
    app.add_middleware(NonFiniteLiteralRefusal)
    app.add_middleware(CallerLogMiddleware, server=name, revision=server_revision())
    # Added after the logger, so Starlette's add-order (most recent outermost) puts the credential
    # check outside it: an unauthenticated request is refused before anything logs or reads it.
    app.add_middleware(BearerAuthMiddleware, server=name, token_env=token_env)
    if max_request_bytes:
        app.add_middleware(BodySizeLimit, max_bytes=max_request_bytes)
    # Last, therefore outermost: the request counter must see the 401s and 413s the layers inside
    # refuse.
    app.add_middleware(RequestMetrics, server=name)

    @app.get("/healthz")
    async def healthz() -> Response:
        """Readiness: can this pod answer tool calls.

        Reports `contract_version` when its manifest declares one. Runs the server's `readiness`
        callable: 503 with the reason on failure, else the corpora it verified. Datasets load
        lazily, so this route — not the import — is where a bad corpus shows.

        - The check runs off the event loop on its own one-thread pool, single-flighted, and a
          failure is memoised for `READINESS_FAILURE_TTL_SECONDS`.
        - The reason is redacted once, before it is memoised: this route is unauthenticated.
        - Only a cause in `degradation.PERMANENT_CAUSES` yields 503; a transient one answers 200
          with `degraded` and is counted (see `verdict`).
        """
        nonlocal readiness_failure
        payload: dict[str, object] = {
            "status": "ok",
            "server": name,
            "revision": server_revision(),
            # Every resource bound this process resolved, at its running value, so an override the
            # shipped files cannot see is visible. Numbers and variable names only; `/healthz` is
            # open.
            "bounds": effective_bounds(),
        }
        declared = _declared_contract_version(name)
        if declared is not None:
            payload["contract_version"] = declared
        if readiness is None:
            READY.labels(name).set(1)
            return JSONResponse(payload)

        def unready(reason: str) -> Response:
            """The 503, with the same redacted reason the memo holds."""
            READY.labels(name).set(0)
            return JSONResponse({**payload, "status": "unready", "reason": reason}, status_code=503)

        def degraded(reason: str, cause: str) -> Response:
            """200, naming what the probe could not do — the answer a transient cause gets."""
            READY.labels(name).set(1)
            return JSONResponse({**payload, "degraded": cause, "reason": reason})

        def verdict(reason: str, cause: str) -> Response:
            """Which of the two a cause earns: 503 for a permanent cause, 200 `degraded` otherwise.

            Decided here rather than per callable so every server inherits the rule and none can
            unready a pod on a transient cause.
            """
            return unready(reason) if cause in PERMANENT_CAUSES else degraded(reason, cause)

        async with readiness_lock:
            if readiness_failure is not None and time.monotonic() < readiness_failure[0]:
                return verdict(readiness_failure[1], readiness_failure[2])
            try:
                # Off the event loop: the check hashes a corpus or runs a subprocess and would stall
                # every stream.
                verified = list(
                    await asyncio.get_running_loop().run_in_executor(readiness_pool, readiness)
                )
            except Exception as exc:
                # `/healthz` is unauthenticated, so the reason is scrubbed here, once, and the memo
                # holds the redacted string — a cached 503 must not leak what a fresh one would not.
                reason = redact_secrets(str(exc))
                cause = classify(exc)
                readiness_failure = (
                    time.monotonic() + READINESS_FAILURE_TTL_SECONDS,
                    reason,
                    cause,
                )
                if cause not in PERMANENT_CAUSES:
                    # Counted, so a pod left in service under pressure is visible from a scrape.
                    record(server=name, component=READINESS_COMPONENT, cause=cause)
                logger.exception("server %s probe failed (%s): %s", name, cause, exc)
                return verdict(reason, cause)
            readiness_failure = None
        READY.labels(name).set(1)
        payload["datasets"] = [f"{corpus.name}@{corpus.version}" for corpus in verified]
        return JSONResponse(payload)

    @app.get("/livez")
    async def livez() -> Response:
        """Liveness only: is this process still serving HTTP.

        Separate from `/healthz` because a liveness failure kills the container, and a restart
        cannot fix a missing corpus or model. Answering proves the lifespan completed and the loop
        is not wedged; it deliberately consults nothing else. `tests/test_deploy_shape.py` holds
        probes to the two routes.
        """
        return JSONResponse({"status": "alive", "server": name, "revision": server_revision()})

    @app.get("/metrics")
    async def metrics() -> Response:
        """Prometheus exposition. Unauthenticated, so what it may carry is bounded.

        Default process/python collectors plus the fleet's per-tool metrics. It must **never** carry
        anything about a caller — actor, session, correlation id or tool argument. A tool name is
        allowed only clamped to the served surface (`_served_tool_name`); a destination host is
        never a label. `tests/test_connector_app.py` asserts both directions.
        """
        return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    # The same routes under their trailing-slash spelling: the `/` mount would otherwise answer 404.
    # An alias rather than a redirect, because kubelet counts a 3xx as a passing probe.
    app.add_api_route("/healthz/", healthz, methods=["GET"], include_in_schema=False)
    app.add_api_route("/livez/", livez, methods=["GET"], include_in_schema=False)
    app.add_api_route("/metrics/", metrics, methods=["GET"], include_in_schema=False)

    # Mounted last: Starlette matches in definition order, so the routes above win and
    # everything else — notably `/mcp` — falls through to the MCP transport.
    app.mount("/", mcp_app)
    return app
