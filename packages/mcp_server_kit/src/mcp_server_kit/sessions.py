"""Bound MCP sessions in lifetime and number, on top of what upstream's transport already does.

Every `initialize` mints a session (an instance-map entry, a live task, two streams) costing ~57 kB
of RSS. Upstream (`mcp` >= 1.30) closes one that has had no HTTP request in flight for its idle
timeout — a running tool call's request and an open GET stream both hold it — forgets a `DELETE`d
one, and discards one whose opening request it refused. This module adds:

- **This fleet's values for upstream's two bounds** (`MCP_SESSION_IDLE_TIMEOUT_SECONDS`,
  `MCP_MAX_SESSIONS`), so `0` switches either off rather than leaving upstream's default in force.
- **A short lease for a session never used after its handshake.**
- **A ceiling refused promptly with 503 and `Retry-After`**, before any session exists and counted
  on `/metrics`: a timeout cannot stop one caller opening sessions faster than they expire.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections.abc import Iterable
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP
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
    "configure_session_bounds",
    "max_sessions",
    "session_idle_timeout",
    "session_unused_timeout",
]

# Upstream's default, well above the gap between tool calls in one turn. Bounds an orphan's
# lifetime, not a call's: upstream counts a call's request as in flight and holds the deadline off.
DEFAULT_SESSION_IDLE_TIMEOUT_SECONDS = 1800.0

#: How long a session minted and never used again may hold its slot. Any real client's next request
#: promotes it to the idle timeout, which upstream restarts when that request completes, so only
#: aborted handshakes and floods keep this lease — a filled ceiling recovers in a minute, not thirty.
DEFAULT_SESSION_UNUSED_TIMEOUT_SECONDS = 60.0

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


def _drop_terminated_sessions(server: FastMCP) -> None:
    """Forget sessions already terminated, so the ceiling counts only live ones.

    Upstream forgets a `DELETE`d session only after the `DELETE` has been answered, so a handshake
    arriving in that gap would be counted against a corpse. O(live sessions); `_session_owners` is
    left to upstream's own discard, which pops both maps for every session that ends.
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


def _start_minted_sessions_on_the_short_lease(server: FastMCP, *, unused: float) -> None:
    """Give a just-minted session the `unused` lease instead of the full idle timeout.

    The minted id is read off upstream's response, and the lease starts once that response is
    complete; upstream restarts the full countdown when the session's next request completes. A
    session upstream refused is already gone and is skipped, and one that has had a request of its
    own is marked and never shortened.
    """
    manager = server.session_manager
    wrapped = manager.handle_request

    async def handle_request(scope: Scope, receive: Receive, send: Send) -> None:
        if not _would_mint_a_session(scope):
            _mark_session_used(server, _session_id_in(scope.get("headers", [])))
            await wrapped(scope, receive, send)
            return
        minted: list[str] = []

        async def send_and_note_the_session(message: Any) -> None:
            if message.get("type") == "http.response.start":
                session_id = _session_id_in(message.get("headers", []))
                if session_id is not None:
                    minted.append(session_id)
            await send(message)

        await wrapped(scope, receive, send_and_note_the_session)
        for session_id in minted:
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
    """Put the just-minted session on the unused lease, unless it is already in use.

    In use is a request of its own (the marker) or one in flight now, which upstream signals with
    a deadline of `math.inf`; shortening that would close the session under the request.
    """
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
