"""Bound MCP sessions in lifetime and number, and never reap one that is still computing.

Every `initialize` mints a session (an instance-map entry, a live task, two streams), and as
`FastMCP` ships nothing ever removes one: no idle timeout is passed, and upstream skips its own
cleanup for a session a `DELETE` terminated. Each un-deleted session costs ~57 kB of RSS. This
module adds:

- **An idle timeout** (`MCP_SESSION_IDLE_TIMEOUT_SECONDS`), held off while any tool call on the
  session runs — and re-asserted after upstream pushes the deadline on every request — because
  expiring a session mid-call silently cuts the SSE stream with no error.
- **A short lease for a session never used after its handshake**, and immediate discard of a
  session upstream minted for a request it refused.
- **A sweep of terminated sessions** after every request.
- **A ceiling** (`MCP_MAX_SESSIONS`): a timeout cannot stop one caller opening sessions faster than
  they expire, so a handshake past the ceiling is refused promptly with **503** and `Retry-After`
  before any session exists.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel.server import request_ctx
from mcp.server.streamable_http import MCP_SESSION_ID_HEADER
from mcp.types import ErrorData, JSONRPCError
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from mcp_server_kit.limits import report_bound
from mcp_server_kit.metrics import SESSIONS_CEILING, SESSIONS_LIVE, SESSIONS_REFUSED

logger = logging.getLogger(__name__)

__all__ = [
    "AT_CAPACITY_RETRY_AFTER_SECONDS",
    "AT_CAPACITY_STATUS",
    "DEFAULT_MAX_SESSIONS",
    "DEFAULT_SESSION_IDLE_TIMEOUT_SECONDS",
    "DEFAULT_SESSION_UNUSED_TIMEOUT_SECONDS",
    "REFUSAL_LOG_INTERVAL_SECONDS",
    "SESSION_BACKLOG_BUDGET_BYTES",
    "SESSION_COST_BYTES",
    "SMALLEST_POD_MEMORY_LIMIT_BYTES",
    "apply_session_ceiling",
    "apply_session_idle_timeout",
    "max_sessions",
    "session_idle_timeout",
    "session_unused_timeout",
]

# Upstream's recommendation, well above the gap between tool calls in one turn. Bounds an orphan's
# lifetime, not a call's: a call in flight holds the deadline off.
DEFAULT_SESSION_IDLE_TIMEOUT_SECONDS = 1800.0

#: How long a session minted and never used again may hold its slot. Any real client's next request
#: promotes it to the idle timeout via upstream's own push, so only aborted handshakes and floods
#: keep this lease — which makes a filled ceiling recover in a minute, not thirty.
DEFAULT_SESSION_UNUSED_TIMEOUT_SECONDS = 60.0

# Marks a transport whose request handler already re-asserts the hold, so a second concurrent
# call on the same session does not stack a second wrapper on top of the first.
_REASSERTED = "_chemclaw_hold_reasserted"

# Marks a session that has had at least one request of its own, so the short first lease
# `_settle_a_minted_session` applies at mint is never applied over an active one.
_USED = "_chemclaw_session_used"


#: RSS cost of one un-deleted session, in bytes: measured ~56.6 kB on `chem`, linear in count; a
#: used session converges on the same marginal cost. The conservative end is used.
SESSION_COST_BYTES = 57_900

#: The smallest pod memory limit in the fleet, which the default must be safe on;
#: `tests/test_fleet_deploy.py::
#: test_the_session_ceiling_is_derived_from_the_smallest_pod_this_fleet_actually_ships` re-reads
#: it from the shipped Deployments.
SMALLEST_POD_MEMORY_LIMIT_BYTES = 512 * 1024 * 1024

#: How much of that limit a backlog of sessions may hold. Sessions are leftovers, not work; a fully
#: abandoned pod refusing handshakes is cheaper than an OOMKill taking every session with it.
SESSION_BACKLOG_BUDGET_BYTES = SMALLEST_POD_MEMORY_LIMIT_BYTES // 8

#: The ceiling on concurrent sessions: the budget over the per-session cost, rounded down.
#: `MCP_MAX_SESSIONS` overrides it; `0` turns it off. `tests/test_sessions.py` re-derives it.
DEFAULT_MAX_SESSIONS = 1024

#: A refused handshake's status — the only reason `/mcp` returns 503 — with `Retry-After`
#: marking it transient.
AT_CAPACITY_STATUS = 503

#: `Retry-After` for a refused caller, in seconds. Long enough that a full ceiling of retrying
#: callers costs the pod under a tenth of a core (a refusal is ~0.8 ms), short enough that a merely
#: busy pod's freed slots are reused promptly.
AT_CAPACITY_RETRY_AFTER_SECONDS = 10

#: How often a refusing pod logs it; the counter is exact per refusal.
REFUSAL_LOG_INTERVAL_SECONDS = 10.0

#: A server-defined JSON-RPC code; upstream's -32600 means "session not found", which a full pod
#: must not be confused with.
_AT_CAPACITY_CODE = -32000


def session_idle_timeout() -> float | None:
    """Seconds a session may go unused before it is terminated, or `None` for no reaping.

    `MCP_SESSION_IDLE_TIMEOUT_SECONDS`; `0` restores upstream's unbounded default.
    """
    raw = os.environ.get("MCP_SESSION_IDLE_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_SESSION_IDLE_TIMEOUT_SECONDS
    try:
        seconds = float(raw)
    except ValueError:
        logger.warning(
            "MCP_SESSION_IDLE_TIMEOUT_SECONDS=%r is not a number; using %.0f s",
            raw,
            DEFAULT_SESSION_IDLE_TIMEOUT_SECONDS,
        )
        return DEFAULT_SESSION_IDLE_TIMEOUT_SECONDS
    return seconds if seconds > 0 else None


def session_unused_timeout(idle: float) -> float:
    """Seconds a just-minted session may sit unused before it is reaped.

    `MCP_SESSION_UNUSED_TIMEOUT_SECONDS`, clamped to `idle`; `0` means no separate first lease.
    Unparseable values fall back to the default and log it.
    """
    raw = os.environ.get("MCP_SESSION_UNUSED_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return min(DEFAULT_SESSION_UNUSED_TIMEOUT_SECONDS, idle)
    try:
        seconds = float(raw)
    except ValueError:
        logger.warning(
            "MCP_SESSION_UNUSED_TIMEOUT_SECONDS=%r is not a number; using %.0f s",
            raw,
            DEFAULT_SESSION_UNUSED_TIMEOUT_SECONDS,
        )
        return min(DEFAULT_SESSION_UNUSED_TIMEOUT_SECONDS, idle)
    return idle if seconds <= 0 else min(seconds, idle)


def _current_transport(server: FastMCP) -> tuple[str, Any] | None:
    """The session id and transport of the session this tool call is being served on.

    `None` when unavailable (a direct call in a test, a stateless server, reaping off), and the tool
    then runs unchanged.
    """
    headers = getattr(getattr(request_ctx.get(None), "request", None), "headers", None)
    if headers is None:
        return None
    session_id = headers.get(MCP_SESSION_ID_HEADER)
    if not session_id:
        return None
    try:
        instances = server.session_manager._server_instances
    except RuntimeError:  # pragma: no cover - no manager yet, so nothing to hold open
        return None
    transport = instances.get(session_id)
    if transport is None or getattr(transport, "idle_scope", None) is None:
        return None
    return session_id, transport


def _reassert_hold_after_upstreams_push(transport: Any, *, held: Callable[[], bool]) -> None:
    """Put the hold back after every request upstream pushes the deadline for.

    Upstream pushes the deadline in `_handle_stateful_request` and then calls
    `transport.handle_request`; wrapping the latter is the only seam after the push and before the
    request is served. Installed once per transport, dying with it; `held` is read at request time.
    """
    if getattr(transport, _REASSERTED, False):
        return
    wrapped = transport.handle_request

    async def handle_request(*args: Any, **kwargs: Any) -> Any:
        scope = getattr(transport, "idle_scope", None)
        if scope is not None and held():
            scope.deadline = math.inf
        return await wrapped(*args, **kwargs)

    transport.handle_request = handle_request
    setattr(transport, _REASSERTED, True)


def _hold_open_during_tool_calls(server: FastMCP, *, timeout: float) -> None:
    """Suspend a session's idle deadline while any tool call on it is running.

    Counted per session, so concurrent calls restore the deadline only when the last returns; the
    re-assert reads the same count.
    """
    manager = getattr(server, "_tool_manager", None)
    if manager is None:  # pragma: no cover - see `app._bind_caller_per_tool_call`
        return
    wrapped = manager.call_tool
    in_flight: dict[str, int] = {}

    async def call_tool(*args: Any, **kwargs: Any) -> Any:
        current = _current_transport(server)
        if current is None:
            return await wrapped(*args, **kwargs)
        session_id, transport = current
        in_flight[session_id] = in_flight.get(session_id, 0) + 1
        _reassert_hold_after_upstreams_push(transport, held=lambda: bool(in_flight.get(session_id)))
        transport.idle_scope.deadline = math.inf
        try:
            return await wrapped(*args, **kwargs)
        finally:
            remaining = in_flight[session_id] - 1
            if remaining:
                in_flight[session_id] = remaining
            else:
                del in_flight[session_id]
                transport.idle_scope.deadline = anyio.current_time() + timeout

    manager.call_tool = call_tool


def _drop_terminated_sessions(server: FastMCP) -> None:
    """Reclaim sessions a polite `DELETE` terminated, which upstream's own cleanup will not.

    Upstream removes a session only if it is *not* `is_terminated`, and `terminate()` sets that flag
    first, so a `DELETE`d session stays in the map forever. A sweep, not a hook on `terminate`, so
    it also covers sessions that never called a tool; O(live sessions).
    """
    try:
        instances = server.session_manager._server_instances
    except RuntimeError:  # pragma: no cover - no manager yet, so nothing to reclaim
        return
    for session_id in [
        session_id
        for session_id, transport in instances.items()
        if getattr(transport, "is_terminated", False)
    ]:
        instances.pop(session_id, None)


def _reclaim_after_every_request(server: FastMCP) -> None:
    """Sweep terminated sessions once per request, on the manager's own ASGI entry.

    Every request passes here, so the request that terminates a session also reclaims it.
    """
    manager = server.session_manager
    wrapped = manager.handle_request

    async def handle_request(*args: Any, **kwargs: Any) -> Any:
        try:
            return await wrapped(*args, **kwargs)
        finally:
            _drop_terminated_sessions(server)

    manager.handle_request = handle_request  # type: ignore[method-assign]


async def _discard_an_unusable_session(server: FastMCP, session_id: str) -> None:
    """Close a session upstream minted for a request it then refused.

    Upstream mints whenever the session-id header is absent, so a bare `DELETE`/`GET`, a sessionless
    `ping` or a malformed body each answer 400 and leave an unusable session that would fill the
    ceiling. The 400 carries the minted `mcp-session-id`, so it is named exactly. `terminate()` then
    `pop`: both are needed to end the task and free the entry.
    """
    try:
        instances = server.session_manager._server_instances
    except RuntimeError:  # pragma: no cover - no manager yet, so nothing to discard
        return
    transport = instances.get(session_id)
    if transport is None:  # pragma: no cover - already swept
        return
    await transport.terminate()
    instances.pop(session_id, None)


def _settle_a_minted_session(server: FastMCP, *, unused: float | None) -> None:
    """Give a just-minted session a short lease, and discard one that was minted by mistake.

    Decided from upstream's response: an error response's session is discarded; a served one starts
    on the `unused` lease, which upstream's push on the next request promotes to the idle timeout.
    `unused=None` installs the discard alone, which is needed even with reaping off. A session that
    has received any further request is marked and never shortened, so a call started immediately
    after the handshake cannot have its hold overwritten.
    """
    manager = server.session_manager
    wrapped = manager.handle_request

    async def handle_request(scope: Scope, receive: Receive, send: Send) -> None:
        if not _would_mint_a_session(scope):
            _mark_session_used(server, _session_id_in(scope.get("headers", [])))
            await wrapped(scope, receive, send)
            return
        minted: list[tuple[str, int]] = []

        async def send_and_note_the_session(message: Any) -> None:
            if message.get("type") == "http.response.start":
                session_id = _session_id_in(message.get("headers", []))
                if session_id is not None:
                    minted.append((session_id, int(message["status"])))
            await send(message)

        await wrapped(scope, receive, send_and_note_the_session)
        for session_id, status in minted:
            if status >= 400:
                await _discard_an_unusable_session(server, session_id)
            elif unused is not None:
                _start_the_short_lease(server, session_id, unused=unused)

    manager.handle_request = handle_request  # type: ignore[method-assign]


def _mark_session_used(server: FastMCP, session_id: str | None) -> None:
    """Record that a session has had a request of its own, so its lease is no longer the first."""
    if session_id is None:
        return
    try:
        transport = server.session_manager._server_instances.get(session_id)
    except RuntimeError:  # pragma: no cover - no manager yet
        return
    if transport is not None:
        setattr(transport, _USED, True)


def _start_the_short_lease(server: FastMCP, session_id: str, *, unused: float) -> None:
    """Put the just-minted session on the unused lease, unless it is already in use."""
    try:
        transport = server.session_manager._server_instances.get(session_id)
    except RuntimeError:  # pragma: no cover - no manager yet
        return
    scope = getattr(transport, "idle_scope", None)
    if scope is None or getattr(transport, _USED, False) or scope.deadline == math.inf:
        return
    scope.deadline = anyio.current_time() + unused


def max_sessions() -> int | None:
    """How many sessions this process will hold at once, or `None` for no ceiling.

    `MCP_MAX_SESSIONS`; `0` is unbounded. A non-integer falls back to the default and logs it.
    """
    raw = os.environ.get("MCP_MAX_SESSIONS", "").strip()
    if not raw:
        return DEFAULT_MAX_SESSIONS
    try:
        ceiling = int(raw)
    except ValueError:
        logger.warning("MCP_MAX_SESSIONS=%r is not an integer; using %d", raw, DEFAULT_MAX_SESSIONS)
        return DEFAULT_MAX_SESSIONS
    return ceiling if ceiling > 0 else None


def _would_mint_a_session(scope: Scope) -> bool:
    """Whether this request is one that creates a session rather than using an existing one.

    Upstream mints exactly when the `mcp-session-id` header is absent, whatever the method or body,
    so the header is the whole test and the body is never read.
    """
    return scope.get("type") == "http" and _session_id_in(scope.get("headers", [])) is None


def _session_id_in(headers: Iterable[tuple[bytes, bytes]]) -> str | None:
    """The `mcp-session-id` an ASGI header list carries, or `None`. Case-insensitive, as HTTP is.

    Read from both the request (absent means a mint) and the response (names the minted session).
    """
    for name, value in headers:
        if name.decode("latin-1").lower() == MCP_SESSION_ID_HEADER.lower():
            return value.decode("latin-1")
    return None


async def _refuse(scope: Scope, receive: Receive, send: Send, *, ceiling: int) -> None:
    """Answer one handshake with "this pod is full", promptly and in terms a caller can act on."""
    body = JSONRPCError(
        jsonrpc="2.0",
        id="server-error",
        error=ErrorData(
            code=_AT_CAPACITY_CODE,
            message=(
                f"this server is already holding {ceiling} MCP sessions, which is its configured "
                "ceiling, so this handshake was refused rather than queued: a session is memory "
                "held until its client says goodbye or the idle timeout reaps it, and admitting "
                "past the ceiling would exhaust the pod and take every session on it down "
                "together. Retry — sessions are reclaimed as callers finish — or raise "
                "MCP_MAX_SESSIONS on a pod with more memory."
            ),
        ),
    )
    response = Response(
        body.model_dump_json(by_alias=True, exclude_none=True),
        status_code=AT_CAPACITY_STATUS,
        media_type="application/json",
        headers={"Retry-After": str(AT_CAPACITY_RETRY_AFTER_SECONDS)},
    )
    await response(scope, receive, send)


def apply_session_ceiling(server: FastMCP, *, name: str) -> int | None:
    """Refuse a new session once `max_sessions()` of them are already open; return the ceiling.

    Installed on the session manager's ASGI entry (after `streamable_http_app()`), before upstream
    mints anything, so a refusal costs nothing. Terminated sessions are swept first. Upstream
    registers a session several awaits later, so an admitted handshake holds a reservation — taken
    under a `threading.Lock` with no await between check and increment — until the response starts
    or a `finally` releases it; otherwise the count is the instance map's, so nothing can leak a
    slot. Returns `None` when the ceiling is off.
    """
    ceiling = max_sessions()
    # Reported whichever way it resolved: `None` on `/healthz` is how a pod whose ceiling an
    # operator turned off says so, which a default in the shipped files cannot.
    report_bound("MCP_MAX_SESSIONS", ceiling)
    if ceiling is None:
        # No gauge is published either: a `chemclaw_mcp_sessions_ceiling` of 0 would read as "this
        # pod admits nothing", which is the opposite of what turning the ceiling off means.
        return None
    SESSIONS_CEILING.labels(name).set(ceiling)
    manager = server.session_manager
    wrapped = manager.handle_request
    lock = threading.Lock()
    reserved = 0
    warned_at = -REFUSAL_LOG_INTERVAL_SECONDS

    def warn_that_the_pod_is_full() -> None:
        """Say the pod is full, at most once per `REFUSAL_LOG_INTERVAL_SECONDS`.

        The exact count is `chemclaw_mcp_sessions_refused_total`; a line per refusal would bury the
        log.
        """
        nonlocal warned_at
        now = time.monotonic()
        with lock:
            if now - warned_at < REFUSAL_LOG_INTERVAL_SECONDS:
                return
            warned_at = now
        logger.warning(
            "server %s: refusing session handshakes; all %d sessions are open, clients are being "
            "told to retry in %d s, and this line is printed at most every %.0f s — "
            "chemclaw_mcp_sessions_refused_total counts every one",
            name,
            ceiling,
            AT_CAPACITY_RETRY_AFTER_SECONDS,
            REFUSAL_LOG_INTERVAL_SECONDS,
        )

    def take_a_slot() -> bool:
        """Claim one slot for a handshake, or report that the pod is full. Never awaits."""
        nonlocal reserved
        with lock:
            _drop_terminated_sessions(server)
            live = len(manager._server_instances)
            SESSIONS_LIVE.labels(name).set(live)
            if live + reserved >= ceiling:
                return False
            reserved += 1
            return True

    async def handle_request(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal reserved
        if not _would_mint_a_session(scope):
            await wrapped(scope, receive, send)
            SESSIONS_LIVE.labels(name).set(len(manager._server_instances))
            return
        if not take_a_slot():
            SESSIONS_REFUSED.labels(name).inc()
            warn_that_the_pod_is_full()
            await _refuse(scope, receive, send, ceiling=ceiling)
            return
        handed_over = False

        def hand_over() -> None:
            """Give the slot to `_server_instances`, which upstream has by now written to."""
            nonlocal reserved, handed_over
            if not handed_over:
                handed_over = True
                with lock:
                    reserved -= 1

        async def send_and_hand_over(message: Any) -> None:
            if message.get("type") == "http.response.start":
                hand_over()
            await send(message)

        try:
            await wrapped(scope, receive, send_and_hand_over)
        finally:
            hand_over()
            SESSIONS_LIVE.labels(name).set(len(manager._server_instances))

    manager.handle_request = handle_request  # type: ignore[method-assign]
    return ceiling


def apply_session_idle_timeout(server: FastMCP) -> float | None:
    """Give `server`'s session manager an idle timeout, and hold it off during tool calls.

    Called after `streamable_http_app()` builds the manager; sets the attribute rather than
    rebuilding, so upstream's other constructor arguments are untouched. Stateless servers are
    skipped (upstream refuses the combination).

    Returns:
        The timeout applied, or `None` if reaping is off or the server is stateless. With reaping
        off the discard of mis-minted sessions is still installed.
    """
    if server.settings.stateless_http:  # pragma: no cover - no stateless server in this fleet
        return None
    timeout = session_idle_timeout()
    report_bound("MCP_SESSION_IDLE_TIMEOUT_SECONDS", timeout)
    if timeout is None:
        # Reaping off still installs the discard: a mis-minted session is reachable by neither the
        # idle deadline nor the ceiling's sweep, and would fill the ceiling permanently.
        _settle_a_minted_session(server, unused=None)
        return None
    server.session_manager.session_idle_timeout = timeout
    _hold_open_during_tool_calls(server, timeout=timeout)
    _reclaim_after_every_request(server)
    unused = session_unused_timeout(timeout)
    report_bound("MCP_SESSION_UNUSED_TIMEOUT_SECONDS", unused)
    _settle_a_minted_session(server, unused=unused)
    return timeout
