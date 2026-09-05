"""Reap MCP sessions nobody is using, and never reap one that is still computing.

`FastMCP` runs stateful streamable-HTTP: every `initialize` mints a session that holds an entry in
`StreamableHTTPSessionManager._server_instances`, a live anyio task blocked in `Server.run(...)`,
and two memory object streams. Upstream will expire an idle one — `StreamableHTTPSessionManager`
takes `session_idle_timeout`, its own docstring recommends 1800 s "for most deployments" — but
`FastMCP.streamable_http_app()` constructs the manager without passing it, and the parameter
defaults to `None`. So in this fleet, as shipped, **a session was removed only by an explicit
`DELETE`**.

That is a leak with a real arrival rate rather than a theoretical one. Chemclaw3 opens a session
per turn per connector, so a pod sees roughly one session open per second at 200 users, and the
client's `DELETE` is in a `finally` that a cancelled turn, a front-door restart or a dropped
connection does not reach. Measured on `chem`: 500 sessions opened and never deleted took RSS from
150.3 MB to 223.1 MB — 149 kB each, never recovered — and a session whose client had exited, whose
TCP connection was gone, still answered HTTP 200 ten seconds later. At 512Mi that is an OOMKill
every day or so, on a Deployment with one replica.

**Upstream's "idle" means "no HTTP request arrived", and for this fleet that is not the same thing
as idle.** The deadline is pushed forward when a request for the session arrives; a tool call is
*one* request whose SSE body is written when the work finishes. `servers/calc` runs CREST searches
with a 14,400 s budget, so a plain 1800 s idle timeout would cancel the session — and with it the
calculation — four hours before its own timeout, from inside the transport, while the chemist is
still waiting. So the deadline is held open for the duration of every tool call, and reset when the
last concurrent call on that session returns. `idle` then means what the word says, and the same
timeout is safe for the server that answers in microseconds and the one that answers in hours.

**Holding it off from inside the call is not enough, and believing it was is what this module
shipped.** Upstream pushes the deadline on *every* request for an existing session — a ping, a
`tools/list`, a reconnecting GET stream — at
`StreamableHTTPSessionManager._handle_stateful_request`, which overwrites `math.inf` with
`now + timeout` and hands the calculation exactly the cancellation the hold-open exists to prevent.
Measured with a 1 s timeout and a 2.5 s tool: the call answers in 2.5 s when nothing else speaks on
the session, and is cut at ~1.5 s with **zero bytes and no JSON-RPC error** if anything does. The
trigger is this family's own client: Chemclaw3's `core/mcp_session.py` sends a `PingRequest` as a
cancellation flush when one call of a fan-out times out, so a `calc` session doing what it was
designed to do would destroy the CREST search running beside it. So the hold is *re-asserted* after
upstream's push, on the transport's own request handler — the one seam that runs after the push and
before the request is served — for as long as that session has a call in flight.

**And the failure it prevents is a hang, not an error**, which is why it is part of this fix rather
than a refinement of it. Measured against a server with the timeout set and no hold-open: expiring
the session cancels `Server.run` and terminates the transport, so the SSE stream the `tools/call`
was being answered on just stops. No JSON-RPC error is ever written. The caller waits until *its*
own timeout with no idea anything happened, and the only trace on the server is one line reading
"idle timeout". `tests/test_sessions.py` drives that counterfactual, so the hold-open tests
beside it are evidence that the timeout is armed rather than that it is absent.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Callable
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel.server import request_ctx
from mcp.server.streamable_http import MCP_SESSION_ID_HEADER

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_SESSION_IDLE_TIMEOUT_SECONDS",
    "apply_session_idle_timeout",
    "session_idle_timeout",
]

# Upstream's own recommendation, and comfortably longer than the gap between two tool calls in one
# Chemclaw3 turn. It bounds how long an orphaned session occupies a pod, not how long a call may
# take — a call in flight holds the deadline off entirely (see `_hold_open_during_tool_calls`).
DEFAULT_SESSION_IDLE_TIMEOUT_SECONDS = 1800.0

# Marks a transport whose request handler already re-asserts the hold, so a second concurrent
# call on the same session does not stack a second wrapper on top of the first.
_REASSERTED = "_chemclaw_hold_reasserted"


def session_idle_timeout() -> float | None:
    """Seconds a session may go unused before it is terminated, or `None` for no reaping.

    `MCP_SESSION_IDLE_TIMEOUT_SECONDS` is the knob; `0` restores upstream's unbounded default,
    which is the shape a deployment would want only to reproduce a leak deliberately.
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


def _current_transport(server: FastMCP) -> tuple[str, Any] | None:
    """The session id and transport of the session this tool call is being served on.

    `request_ctx` is set per JSON-RPC message and carries the ASGI request, which is where the
    session id lives; the transport holding the idle scope is only reachable through the session
    manager's own instance map. Returns `None` whenever any of that is absent — a direct call in a
    test, a stateless server, or a deployment that has turned reaping off — so the caller runs the
    tool unchanged.
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

    Upstream's push runs at the top of `_handle_stateful_request` and then delegates to
    `transport.handle_request`, so wrapping *that* is the only seam that runs after the deadline has
    been overwritten and before the request is served. Wrapping the manager instead would run
    before the push and be undone by it, which is the shape of the defect rather than its fix.

    Installed once per transport, when that session first takes a hold, and it dies with the
    transport — a set of session ids in a closure would outlive them, which is the leak this module
    exists to close. `held` is read at request time, not at install time: the wrapper stays for the
    life of the session and writes nothing once the last call has returned.
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

    Wraps `_tool_manager.call_tool`, the seam `connector_app`'s other per-call behaviours already
    use, and lands outside them because the session manager it needs only exists once
    `streamable_http_app()` has been called. Ordering is immaterial here: every one of those
    wrappers brackets the same tool body, and this one is not describing the call, only keeping its
    session alive while it runs.

    The count is per session because one session may carry concurrent calls: the deadline is
    restored by the last one to return, not the first. It is also what the re-assert reads, so the
    two writers agree on one number rather than each keeping its own.
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


def apply_session_idle_timeout(server: FastMCP) -> float | None:
    """Give `server`'s session manager an idle timeout, and hold it off during tool calls.

    Called after `FastMCP.streamable_http_app()`, which is what constructs the manager lazily.
    Setting the attribute rather than rebuilding the manager with the keyword is deliberate: every
    other constructor argument is upstream's business and restating them here would silently drop
    whichever one upstream adds next. Both readers of `session_idle_timeout` — the request handler
    that pushes the deadline and the session task that arms it — read the attribute at run time.

    Stateless servers are skipped: upstream refuses the combination outright, because a stateless
    transport keeps no session to expire. No server in this fleet is stateless today.

    Returns:
        The timeout applied, or `None` if reaping is off or the server is stateless.
    """
    timeout = session_idle_timeout()
    if timeout is None:
        return None
    if server.settings.stateless_http:  # pragma: no cover - no stateless server in this fleet
        return None
    server.session_manager.session_idle_timeout = timeout
    _hold_open_during_tool_calls(server, timeout=timeout)
    return timeout
