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

**And the failure it prevents is a hang, not an error**, which is why it is part of this fix rather
than a refinement of it. Measured against a server with the timeout set and no hold-open: expiring
the session cancels `Server.run` and terminates the transport, so the SSE stream the `tools/call`
was being answered on just stops. No JSON-RPC error is ever written. The caller waits until *its*
own timeout with no idea anything happened, and the only trace on the server is one line reading
"idle timeout". `tests/test_sessions.py` drives both arms.
"""

from __future__ import annotations

import logging
import math
import os
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


def _current_session(server: FastMCP) -> tuple[str, Any] | None:
    """The session id and idle scope of the session this tool call is being served on.

    `request_ctx` is set per JSON-RPC message and carries the ASGI request, which is where the
    session id lives; the transport holding the scope is only reachable through the session
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
    scope = getattr(instances.get(session_id), "idle_scope", None)
    return None if scope is None else (session_id, scope)


def _hold_open_during_tool_calls(server: FastMCP, *, timeout: float) -> None:
    """Suspend a session's idle deadline while any tool call on it is running.

    Wraps `_tool_manager.call_tool`, the seam `connector_app`'s other per-call behaviours already
    use, and lands outside them because the session manager it needs only exists once
    `streamable_http_app()` has been called. Ordering is immaterial here: every one of those
    wrappers brackets the same tool body, and this one is not describing the call, only keeping its
    session alive while it runs.

    The count is per session because one session may carry concurrent calls: the deadline is
    restored by the last one to return, not the first. Upstream pushes the same deadline forward on
    every incoming request, so the two agree — this only covers the interval upstream cannot see,
    between a call's request arriving and its result being written.
    """
    manager = getattr(server, "_tool_manager", None)
    if manager is None:  # pragma: no cover - see `app._bind_caller_per_tool_call`
        return
    wrapped = manager.call_tool
    in_flight: dict[str, int] = {}

    async def call_tool(*args: Any, **kwargs: Any) -> Any:
        current = _current_session(server)
        if current is None:
            return await wrapped(*args, **kwargs)
        session_id, scope = current
        in_flight[session_id] = in_flight.get(session_id, 0) + 1
        scope.deadline = math.inf
        try:
            return await wrapped(*args, **kwargs)
        finally:
            remaining = in_flight[session_id] - 1
            if remaining:
                in_flight[session_id] = remaining
            else:
                del in_flight[session_id]
                scope.deadline = anyio.current_time() + timeout

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
