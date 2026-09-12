"""Reap MCP sessions nobody is using, and never reap one that is still computing.

`FastMCP` runs stateful streamable-HTTP: every `initialize` mints a session that holds an entry in
`StreamableHTTPSessionManager._server_instances`, a live anyio task blocked in `Server.run(...)`,
and two memory object streams. Upstream will expire an idle one — `StreamableHTTPSessionManager`
takes `session_idle_timeout`, its own docstring recommends 1800 s "for most deployments" — but
`FastMCP.streamable_http_app()` constructs the manager without passing it, and the parameter
defaults to `None`. So in this fleet, as shipped, **nothing removed a session at all** — and the
sentence here used to say "removed only by an explicit `DELETE`", which measured false in the
direction that made nobody look. A `DELETE` does not remove one either: upstream's only
unconditional removal is guarded by `not http_transport.is_terminated`, and `terminate()` sets
that flag first, so the polite close takes the branch that skips it. `_drop_terminated_sessions`
is the half that was missing; the timeout below is the half for a client that never says goodbye.

That is a leak with a real arrival rate rather than a theoretical one. Chemclaw3 opens a session
per turn per connector, so a pod sees roughly one session open per second at 200 users, and the
client's `DELETE` is in a `finally` that a cancelled turn, a front-door restart or a dropped
connection does not reach. Measured on `chem`: 500 sessions opened and never deleted took RSS from
138.3 MB to 166.0 MB — **56.6 kB each**, never recovered — and a session whose client had exited,
whose TCP connection was gone, still answered HTTP 200 ten seconds later.

(That figure read **149 kB** here until it was re-measured for the ceiling below, against the same
server by the same method, and it is corrected rather than quietly dropped. The direction matters
in both halves: the leak is two and a half times cheaper per session than this file claimed, which
makes the *reaper* less urgent than it read — and the ceiling below more so, because the number of
sessions one caller can hold inside a 512Mi pod is two and a half times larger than anyone
reading this paragraph would have assumed.)

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

**A timeout bounds how long a session lives and nothing bounded how many there are**, which is a
different control and the one this module was still missing. `_server_instances` is a plain dict
that upstream adds to on every `initialize` with no admission of any kind: a session's cost is paid
at creation and refunded, at the earliest, `MCP_SESSION_IDLE_TIMEOUT_SECONDS` later, so the reaper
sets the *steady state* and the arrival rate sets the *peak*. Measured against the real `chem` app
under uvicorn, the server in its own process so the figures are the pod's and not a client's:
un-deleted sessions cost **56.6 kB each** and grow strictly linearly (55,292 kB over 1,000
sessions, and the per-session figure is flat to three digits at every 200-session mark), and one
client on loopback opens **261 of them per second**. A session that has been *used* — handshake,
GET stream, `tools/list`, one `tools/call`, then abandoned — costs more at first and converges on
the same marginal figure: 126 kB at 50 sessions falling to 87 kB at 300, whose last three
increments are 56, 64 and 60 kB.

Two consequences, and the second is why a ceiling is not optional. At Chemclaw3's own ~1
session/s the reaper holds a fully-abandoned pod at about 1,800 sessions, which is ~102 MB — large
but survivable. At the rate one authenticated caller can actually dial, the same 30-minute window
is hundreds of thousands of sessions: the smallest pod in this fleet is limited to 512Mi and every
one of them is an OOMKill, which takes down every other session sharing the pod, including a CREST
search four hours in. **An idle timeout cannot see that at all**, because nothing has been idle
long enough to reap.

So `MCP_MAX_SESSIONS` is a ceiling on how many sessions exist at once, enforced on the request that
would mint one, and a pod at its ceiling refuses **promptly** rather than queueing — the same
argument `servers/calc/engine/admission.py` makes for a calculation, one layer down. It belongs
here rather than beside that one because a session is the *transport's* object: every server in
this fleet has the same one, pays the same 56.6 kB for it, and none of them can see it from a tool
body. An admission ceiling on calls and a ceiling on sessions are not the same bound and neither
implies the other — `props` gates no call at all and still holds sessions.

**The refusal is an HTTP status, not a worded string, and that is the difference from the tool
path.** `servers/calc` needs `AT_CAPACITY_MARKER` because a refused *tool call* has no channel but
its own text: the protocol flattens every exception into one `isError=True` text block with no
code. A refused handshake is a plain HTTP response on `/mcp`, so it carries a real status —
**503** with `Retry-After` — which no other outcome on that path returns. A client that reads the
status needs nothing from the body; the body says what happened for a human reading a capture.
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

# Upstream's own recommendation, and comfortably longer than the gap between two tool calls in one
# Chemclaw3 turn. It bounds how long an orphaned session occupies a pod, not how long a call may
# take — a call in flight holds the deadline off entirely (see `_hold_open_during_tool_calls`).
DEFAULT_SESSION_IDLE_TIMEOUT_SECONDS = 1800.0

#: How long a session that has been minted and *never used again* may hold its slot. A handshake
#: is not a conversation: upstream pushes the deadline forward on every request for an existing
#: session, so any client that goes on to do anything at all — an MCP client sends
#: `notifications/initialized` microseconds later — is promoted to the full idle timeout by
#: upstream's own code with nothing here to do. What is left holding the short lease is exactly
#: the session nobody ever came back for: an aborted handshake, a client that died between
#: `initialize` and its first call, or a flood. Sixty seconds is four orders of magnitude above
#: the gap a real client leaves and thirty times below the idle timeout, which is what turns the
#: ceiling from a thirty-minute wedge into a minute-long one.
DEFAULT_SESSION_UNUSED_TIMEOUT_SECONDS = 60.0

# Marks a transport whose request handler already re-asserts the hold, so a second concurrent
# call on the same session does not stack a second wrapper on top of the first.
_REASSERTED = "_chemclaw_hold_reasserted"

# Marks a session that has had at least one request of its own, so the short first lease
# `_bound_a_new_sessions_first_lease` applies at mint is never applied over an active one.
_USED = "_chemclaw_session_used"


#: What one un-deleted session costs this process, in bytes of RSS. Measured on the real `chem`
#: app under uvicorn in its own process: 1,000 raw `initialize` handshakes, never deleted, took RSS
#: from 133,428 kB to 188,720 kB — 56.6 kB each, flat to three digits at every 200-session mark, so
#: the growth is linear rather than amortising. A *used* session (GET stream, `tools/list`, one
#: `tools/call`) starts higher and converges on the same marginal figure: 126 kB at 50 falling to
#: 87 kB at 300, with its last three 50-session increments at 56, 64 and 60 kB. The conservative
#: half of that range is the one written down.
SESSION_COST_BYTES = 57_900

#: The smallest `resources.limits.memory` any pod in this fleet runs under — `props`, `chem` and
#: `safety` are all 512Mi. It is the pod a fleet-wide default has to be safe on, and
#: `tests/test_fleet.py` re-reads it from the shipped Deployments rather than trusting this line,
#: because a server whose limit is lowered below it moves the derivation below.
SMALLEST_POD_MEMORY_LIMIT_BYTES = 512 * 1024 * 1024

#: How much of that limit a *backlog of sessions* may hold. A session is overhead, not work: it is
#: what is left over from turns nobody finished, and the pod's memory is for the corpus it serves
#: and the calls in flight. An eighth is the budget, which leaves the reaper's own steady state
#: (~1,800 sessions at Chemclaw3's ~1/s arrival and a 1,800 s timeout) above the ceiling — that is
#: deliberate and is the trade this bound exists to make: a pod that is *fully* abandoned refuses
#: new handshakes, and losing one turn is cheaper than the OOMKill that takes every session in the
#: pod with it, a four-hour CREST search included.
SESSION_BACKLOG_BUDGET_BYTES = SMALLEST_POD_MEMORY_LIMIT_BYTES // 8

#: The ceiling on concurrent sessions, derived from the two numbers above and rounded *down* to a
#: round figure so the budget is a bound rather than a target: 1,024 sessions is 59.3 MB against a
#: 64 MiB budget. Overridable with `MCP_MAX_SESSIONS`; `0` turns the ceiling off, which is the
#: shape a deployment would choose only to reproduce the unbounded behaviour deliberately.
#: `tests/test_sessions.py` re-derives it, so lowering `SESSION_COST_BYTES` or raising this
#: without the other cannot pass.
DEFAULT_MAX_SESSIONS = 1024

#: What a refused handshake answers with. 503 is the only status `/mcp` returns for this reason —
#: upstream answers 404 for an unknown session, 200 for a served one, 401/413 from the middleware
#: above — so a client needs nothing from the body to tell the cases apart. `Retry-After` says the
#: refusal is transient, which is the one thing a caller can act on.
AT_CAPACITY_STATUS = 503

#: What `Retry-After` tells a refused caller, in seconds. It shipped at **1**, which is the one
#: value the pod cannot honour: the slots a full pod is holding are refunded no earlier than
#: `MCP_SESSION_UNUSED_TIMEOUT_SECONDS` (a handshake nobody followed up) or
#: `MCP_SESSION_IDLE_TIMEOUT_SECONDS` (a conversation that stopped), and until then every retry is
#: refused again. Derived rather than chosen: a refusal costs this pod 0.8 ms of its own CPU
#: (measured over one shared connection against the real app, 50 refusals, median 0.792 ms), so
#: `DEFAULT_MAX_SESSIONS` displaced callers all retrying on this interval spend
#: `1024 x 0.0008 / R` of a core on being told no. At the shipped 1 s that is **0.8 of a core** —
#: on a pod limited to two, the refusal path becomes the load and starves the sessions that are
#: still working. 10 s holds it under a tenth of a core, and is short enough that a caller whose
#: turn is still worth having has not given up. It is *not* raised to the reclaim horizon: a pod
#: that is merely busy frees slots as callers finish, in seconds, and a minute-long `Retry-After`
#: would turn a two-second saturation into a minute-long outage for every client that obeyed it.
AT_CAPACITY_RETRY_AFTER_SECONDS = 10

#: How often a pod that is refusing handshakes says so in its log. The counter is exact and
#: per-refusal; the log is for a human, and at the arrival rates this ceiling exists to survive a
#: line per refusal is hundreds a second of one repeated sentence.
REFUSAL_LOG_INTERVAL_SECONDS = 10.0

#: JSON-RPC reserves -32000..-32099 for server-defined errors. Upstream spends `INVALID_REQUEST`
#: (-32600) on "session not found", and re-using it would make a full pod indistinguishable from a
#: stale session id in the body — which is exactly the confusion the status code avoids, so the
#: body must not reintroduce it.
_AT_CAPACITY_CODE = -32000


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


def session_unused_timeout(idle: float) -> float:
    """Seconds a just-minted session may sit unused before it is reaped.

    `MCP_SESSION_UNUSED_TIMEOUT_SECONDS` is the knob. It is clamped to `idle` rather than allowed
    to exceed it — a "first lease" longer than the ordinary one is not a shorter lease, and a
    deployment that wants the old behaviour asks for it by setting the two equal. Unparseable
    values fall back and say so, the shape both knobs above use; `0` means "no separate first
    lease", which is that same request spelled differently.
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


def _drop_terminated_sessions(server: FastMCP) -> None:
    """Reclaim sessions a polite `DELETE` terminated, which upstream's own cleanup will not.

    **The reaper closes the abandoned half and nothing closed the polite one**, which is the
    happy path: Chemclaw3 sends its `DELETE` from a `finally`. Upstream's only unconditional
    removal is in `run_server`'s `finally`, guarded by `and not http_transport.is_terminated` —
    and `terminate()` sets that flag as its first statement, so the DELETE path takes exactly the
    branch that skips the `del`. The entry, its transport and everything they hold stay in
    `_server_instances` for the life of the process, and no idle deadline can reach them because
    the scope's task is already gone.

    Measured before this: 300 sessions, each `initialize` + `tools/call` + `DELETE`, left
    **300 of 300** in the map, every one `is_terminated`. An abandoned session in the same run was
    reaped correctly, which is why the half that works hid the half that does not.

    A sweep rather than a hook on `terminate`, for two reasons. It covers a session that never
    called a tool, which the per-transport wrappers above never see; and it needs no second
    guess about which of upstream's paths set the flag. It is O(live sessions) on a map this
    function is what keeps small, so the cost is bounded by its own effect.
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

    The manager's `handle_request` is the one seam every request passes through — including a
    `DELETE` on a session that never called a tool, which is why this is here rather than on the
    per-transport wrapper `_reassert_hold_after_upstreams_push` installs. After the sweep runs,
    the request that terminated a session is the request that reclaims it.
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

    Upstream mints on the *absence* of the session-id header and nothing else — not the method, not
    the body — so a bare `DELETE /mcp`, a bare `GET /mcp`, a `ping` with no session id and a
    malformed body each answer **400 Bad Request** and each leave a fully-registered session
    behind. Measured against the real app: `live 0 -> 1` for every one of those four, and a bare
    `DELETE` at **476 requests/s**, which fills the shipped 1,024-session ceiling in about two
    seconds using the cheapest request anybody can construct. Not one of those sessions can ever be
    used: the caller was told its request was bad, and a session whose `initialize` never succeeded
    serves nothing.

    They are nameable, which is what makes this exact rather than a heuristic: upstream puts
    `mcp-session-id` on the 400 as well as on the 200, so the response names the session it just
    orphaned and no before/after diff of the instance map — which another request may have written
    to concurrently — is needed.

    `terminate()` first and then the `pop`, in that order and both: the pop alone would leave the
    session's anyio task blocked in `Server.run` forever, and `terminate()` alone is what
    `_drop_terminated_sessions` exists because upstream does not follow with a `del`.
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


def _bound_a_new_sessions_first_lease(server: FastMCP, *, unused: float) -> None:
    """Give a just-minted session a short lease, and discard one that was minted by mistake.

    **A handshake is not a conversation.** The idle timeout is 1,800 s because that is the right
    bound on a session a chemist is between tool calls on; it is three orders of magnitude too
    generous for one that was minted and never spoken to again. That gap is what makes the ceiling
    one caller's denial-of-service rather than a memory bound: measured, 1,024 handshakes take
    about four seconds to open and the slots are refunded no earlier than thirty minutes later,
    during which every other caller is refused. Shortening only the *unused* lease costs a real
    client nothing, because upstream already pushes the deadline to the full timeout on every
    request for an existing session, and an MCP client sends `notifications/initialized`
    microseconds after the handshake returns.

    So this wrapper does two things to a minting request, both decided from the response upstream
    wrote: if it was an error, the session it minted is unusable and is discarded on the spot; if
    it was served, the session starts on the short lease and upstream's own push promotes it.

    A session that has received any request at all is marked, and the short lease is never applied
    to a marked one — that is not decoration, it is what closes the window between the mint
    response being written and this code running. A client that read its session id off that
    response and immediately started a tool call would otherwise have the hold-open deadline
    (`math.inf`) overwritten with 60 s, and the call cancelled from inside the transport with no
    error written anywhere, which is the exact failure `_hold_open_during_tool_calls` exists to
    prevent.
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
            else:
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

    `MCP_MAX_SESSIONS` is the knob; `0` restores the unbounded behaviour, which a deployment would
    ask for only to reproduce the exhaustion this bound exists to stop. A value that is not an
    integer is a misconfiguration rather than a licence to serve unbounded, so it falls back to the
    default and says so — the same shape `session_idle_timeout` uses one function up.
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

    Upstream's `_handle_stateful_request` branches on the `mcp-session-id` header alone: absent, it
    mints a transport and registers it; present, it either serves that session or answers 404. So
    the header is the whole test, and it is read here rather than inferred from the method or the
    JSON-RPC body — a `GET` or a `DELETE` with no session id takes the same minting branch, and
    reading the body would mean consuming the ASGI receive channel before upstream needs it.
    """
    return scope.get("type") == "http" and _session_id_in(scope.get("headers", [])) is None


def _session_id_in(headers: Iterable[tuple[bytes, bytes]]) -> str | None:
    """The `mcp-session-id` an ASGI header list carries, or `None`. Case-insensitive, as HTTP is.

    Used for both directions of one request: the id a caller presents (absent means a session is
    about to be minted) and the id upstream puts on the response (which names the session that
    request just minted, on an error response as much as on a served one).
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
    """Refuse a new session once `max_sessions()` of them are already open.

    Installed on the session manager's own ASGI entry, which is the last seam that runs before
    upstream decides whether to mint a transport — and the only one where a refusal costs nothing,
    because no session, no task and no memory object stream exist yet. That is what makes this
    admission control rather than a clock: nothing is abandoned mid-flight, because nothing was
    started.

    **Terminated sessions are swept before the count is taken, and the reason is narrower than it
    first looks.** `_reclaim_after_every_request` already sweeps after every served request, so on
    the shipped configuration this one finds nothing — a `DELETE` is reclaimed by the request that
    performed it, long before the next handshake counts anything. The case it exists for is
    `MCP_SESSION_IDLE_TIMEOUT_SECONDS=0`: that turns `apply_session_idle_timeout` off entirely, and
    the reclaim sweep goes with it, so without this line a pod whose callers all said goodbye would
    refuse at a ceiling of corpses — a ceiling turning a *supported* configuration into a
    permanently full pod. It is written down this way because it was first written down the other
    way, and a mutation removing the call left every test green.

    The sweep is O(live sessions) on a map this ceiling is what keeps small.

    Wrapped *outside* `_reclaim_after_every_request` so a refused request never reaches it — there
    is nothing to reclaim on a request that was not served, and the sweep above has already run.

    **The slot is taken before the await, not derived from a dict upstream fills afterwards**, and
    the first version of this function got that wrong in the one way that matters. `live` was
    `len(manager._server_instances)` alone, read on the ASGI entry — but upstream registers the
    session inside `_handle_stateful_request`, behind `async with self._session_creation_lock`,
    several awaits later. Every concurrent handshake therefore observed the *pre-burst* count and
    every one of them was admitted. Measured against the real app under uvicorn, all handshakes
    released from one `threading.Barrier`: a ceiling of 8 admitted **64 of 64** and held 64 live
    sessions, and a ceiling of 64 admitted **256 of 256**. A serial caller was bounded correctly,
    which is why every test passed — the saturation probe filled the pod one handshake at a time
    and only burst against an already-full pod, so it never opened the window. A burst is the exact
    adversary the ceiling was written for: one client on loopback opens 261 handshakes a second.

    So an admitted handshake takes a *reservation* — an integer this function owns, incremented
    under a `threading.Lock` with no `await` between the read and the increment, the same shape
    `servers/calc/engine/admission.py` uses one layer down — and the reservation is handed over to
    `_server_instances` the moment upstream sends the response, because upstream registers the
    transport strictly before it can write a byte. `send` is watched for that handover rather than
    the request's return, so a request that holds its connection open (a sessionless `GET`, which
    upstream mints for) does not hold a second slot for the life of the stream.

    **The count stays derived from the map, and that is what keeps a reservation from leaking.**
    The reservation covers only the window between the check and the registration — it is released
    in a `finally` as well, so an exception, a cancellation or a client hang-up cannot strand one —
    and everything after that window is `len(_server_instances)`, which a reap, a `DELETE` or a
    crash empties on its own. A counter that *replaced* the map would have to be decremented on
    every one of those paths, and the one it missed would wedge the pod permanently.

    Args:
        server: The `FastMCP` whose session manager is being bounded. Must already have had
            `streamable_http_app()` called on it, which is what builds that manager.
        name: The server's name, for the metric labels and the one log line an operator reads.

    Returns:
        The ceiling applied, or `None` if a deployment has turned it off.
    """
    ceiling = max_sessions()
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

        A refusal costs the pod 0.8 ms and the client is told to come back in
        `AT_CAPACITY_RETRY_AFTER_SECONDS`, so a pod that is genuinely full is refusing everything
        it displaced — hundreds of times a second at the shipped ceiling. A line each would bury
        every other line in the pod's log, including the ones that say *why* it is full. The exact
        per-refusal number is `chemclaw_mcp_sessions_refused_total`, which is incremented whether
        or not this prints; no count is restated here, because a count of a window that has not
        closed yet is a number nobody can act on.
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
    _reclaim_after_every_request(server)
    _bound_a_new_sessions_first_lease(server, unused=session_unused_timeout(timeout))
    return timeout
