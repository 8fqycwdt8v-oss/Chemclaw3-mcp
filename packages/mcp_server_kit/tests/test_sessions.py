"""An abandoned MCP session is reaped; one with a call still running is not.

`FastMCP` never passes upstream's `session_idle_timeout`, so `sessions.py` sets one. Upstream
pushes the deadline only when a request arrives, so a tool call outliving the timeout would be
cancelled mid-flight; the hold-open wrapper prevents that, and the counterfactual test drives the
same slow tool without it.

Timings are short (1 s timeout, a 2.5 s tool) because the subject is a deadline, not a duration.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import anyio
import httpx
import pytest
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from mcp_server_kit.app import connector_app
from mcp_server_kit.sessions import (
    _USED,
    DEFAULT_SESSION_IDLE_TIMEOUT_SECONDS,
    DEFAULT_SESSION_UNUSED_TIMEOUT_SECONDS,
    _start_the_short_lease,
    session_idle_timeout,
    session_unused_timeout,
)

TOKEN = "test-token-for-sessions"
TOKEN_ENV = "MCP_KIT_SESSIONS_TOKEN"
IDLE_TIMEOUT_SECONDS = 1.0
# Comfortably longer than the idle timeout, so a call that is not held open is certain to be cut.
SLOW_TOOL_SECONDS = 2.5
PROTOCOL_VERSION = "2025-06-18"


def _probe_server(name: str) -> FastMCP:
    """A server with one instant tool and one that outlives the idle timeout on purpose."""
    server = FastMCP(name)

    @server.tool()
    def echo(text: str) -> str:
        """Return what was passed in."""
        return text

    @server.tool()
    async def slow() -> str:
        """Take longer than the session idle timeout, the way a CREST search does."""
        await asyncio.sleep(SLOW_TOOL_SECONDS)
        return "finished"

    return server


@pytest.fixture(autouse=True)
def short_idle_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A one-second timeout, and the bearer token every app in this file requires."""
    monkeypatch.setenv("MCP_SESSION_IDLE_TIMEOUT_SECONDS", str(IDLE_TIMEOUT_SECONDS))
    monkeypatch.setenv(TOKEN_ENV, TOKEN)


def test_the_default_is_upstreams_recommendation_and_zero_turns_reaping_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The knob, including the escape hatch — a deployment may reproduce the old behaviour."""
    monkeypatch.delenv("MCP_SESSION_IDLE_TIMEOUT_SECONDS")
    assert session_idle_timeout() == DEFAULT_SESSION_IDLE_TIMEOUT_SECONDS
    monkeypatch.setenv("MCP_SESSION_IDLE_TIMEOUT_SECONDS", "0")
    assert session_idle_timeout() is None
    monkeypatch.setenv("MCP_SESSION_IDLE_TIMEOUT_SECONDS", "not a number")
    assert session_idle_timeout() == DEFAULT_SESSION_IDLE_TIMEOUT_SECONDS


def _open_session(base: str) -> str:
    """Open an MCP session with a raw POST and return its id, without a client library.

    Deliberately raw: the point of the first test is what happens to a session whose client has
    *gone*, and a client library that tidies up on exit is the one thing that must not happen here.
    """
    response = httpx.post(
        f"{base}/mcp",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "0"},
            },
        },
        timeout=10.0,
    )
    assert response.status_code == 200, response.text
    return response.headers["mcp-session-id"]


def _ping(base: str, session_id: str) -> httpx.Response:
    """A ping on an existing session id — 200 while the session lives, 404 once it is gone."""
    return httpx.post(
        f"{base}/mcp",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "mcp-session-id": session_id,
        },
        json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
        timeout=10.0,
    )


def test_a_session_whose_client_vanished_is_reaped(serving: Callable[..., Any]) -> None:
    """A session whose client vanished is reaped, over a real socket with no client library.

    Alive after the handshake, gone once the idle timeout has passed, with nothing in between.
    """
    app = connector_app(_probe_server("reaped"), name="reaped", token_env=TOKEN_ENV)
    with serving(app) as base:
        session_id = _open_session(base)
        assert _ping(base, session_id).status_code == 200
        time.sleep(IDLE_TIMEOUT_SECONDS * 2 + 1.0)
        reaped = _ping(base, session_id)
        assert reaped.status_code == 404, (
            f"the session was still alive {IDLE_TIMEOUT_SECONDS * 2 + 1.0:.1f} s after its last "
            f"request; MCP_SESSION_IDLE_TIMEOUT_SECONDS was {IDLE_TIMEOUT_SECONDS}"
        )


async def test_a_session_with_a_call_in_flight_is_not_reaped(
    serving: Callable[..., Any], mcp_session: Callable[..., Any]
) -> None:
    """A session with a call in flight is not reaped as idle.

    The 2.5 s tool outlives the 1 s timeout armed when `tools/call` arrived; it still returns, and
    the session remains usable.
    """
    app = connector_app(_probe_server("held-open"), name="held-open", token_env=TOKEN_ENV)
    with serving(app) as base:
        async with mcp_session(base, token=TOKEN) as session:
            result = await session.call_tool("slow", {})
            assert not result.isError, result.content
            assert "finished" in result.content[0].text
            # The deadline is restored when the call returns, not abandoned: the session is still
            # here immediately afterwards, which is what "held open" has to mean.
            still_here = await session.call_tool("echo", {"text": "after"})
            assert not still_here.isError


@asynccontextmanager
async def _unheld_app(name: str) -> AsyncIterator[FastAPI]:
    """The counterfactual: upstream's idle timeout with nothing holding it open.

    Assembled by hand, because `connector_app` is what installs the hold-open wrapper.
    """
    server = _probe_server(name)
    mcp_app = server.streamable_http_app()
    server.session_manager.session_idle_timeout = IDLE_TIMEOUT_SECONDS

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with server.session_manager.run():
            yield

    app = FastAPI(lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.mount("/", mcp_app)
    yield app


async def test_without_the_hold_open_the_caller_never_gets_an_answer(
    serving: Callable[..., Any], mcp_session: Callable[..., Any]
) -> None:
    """Without the hold-open, the caller never gets an answer.

    This arm shows the timeout is armed and that the hold-open is what survives it. The failure is a
    hang, not an error: expiring the session stops the SSE stream with no JSON-RPC error written, so
    `wait_for` bounds the test.
    """
    async with _unheld_app("unheld") as app:
        with serving(app) as base:
            async with mcp_session(base) as session:
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        session.call_tool("slow", {}), timeout=SLOW_TOOL_SECONDS * 2
                    )


@pytest.mark.parametrize("interfering", ["ping", "tools/list"])
async def test_a_second_request_does_not_reap_the_call_in_flight(
    serving: Callable[..., Any], mcp_session: Callable[..., Any], interfering: str
) -> None:
    """A second request on the session does not reap the call in flight.

    Upstream re-arms the deadline on every request for an existing session, including a ping or
    `tools/list`, which would overwrite the hold-open's `math.inf`; the hold-open must re-assert.
    Chemclaw3 sends a ping as a cancellation flush when one call of a fan-out times out, so this is
    a real trigger.
    """
    app = connector_app(_probe_server("interfered"), name="interfered", token_env=TOKEN_ENV)
    with serving(app) as base:
        async with mcp_session(base, token=TOKEN) as session:
            call = asyncio.ensure_future(session.call_tool("slow", {}))
            await asyncio.sleep(IDLE_TIMEOUT_SECONDS / 2)
            if interfering == "ping":
                await session.send_ping()
            else:
                await session.list_tools()
            result = await asyncio.wait_for(call, timeout=SLOW_TOOL_SECONDS * 3)
            assert not result.isError, result.content
            assert "finished" in result.content[0].text


async def test_a_politely_deleted_session_is_reclaimed(
    serving: Callable[..., Any], mcp_session: Callable[..., Any]
) -> None:
    """A politely deleted session is reclaimed.

    `terminate()` sets `is_terminated` first, and upstream's `run_server` `finally` skips the `del`
    for a terminated transport, so every `DELETE`d session would stay in `_server_instances` for the
    life of the process. This is the fleet's happy path. Two sessions, one never calling a tool,
    because the per-transport wrappers are installed from inside `call_tool`.
    """
    server = _probe_server("reclaimed")
    app = connector_app(server, name="reclaimed", token_env=TOKEN_ENV)
    with serving(app) as base:
        instances = server.session_manager._server_instances
        async with mcp_session(base, token=TOKEN) as session:
            await session.call_tool("echo", {"text": "hello"})
        async with mcp_session(base, token=TOKEN):
            pass
        assert instances == {}, (
            f"{len(instances)} session(s) survived their own DELETE: "
            f"{[(sid, getattr(t, 'is_terminated', None)) for sid, t in instances.items()]}; "
            "upstream skips its own cleanup for a terminated transport, so nothing else reclaims "
            "these and the idle deadline cannot reach them"
        )
        # The sweep does not touch `_session_owners`. Sound only because `BearerAuthMiddleware`
        # never sets an `AuthenticatedUser` in `scope["user"]`, so upstream records no owner;
        # `test_upstream_surface.py` pins that condition.
        assert server.session_manager._session_owners == {}, (
            "sessions are being recorded in `_session_owners`, which nothing in this kit sweeps"
        )


#: The two leases the first-lease tests run under. Far apart so a session reaped on one is not a
#: session that might have been reaped on the other, and both short enough to wait out.
FIRST_LEASE_SECONDS = 1.0
FULL_LEASE_SECONDS = 30.0


def test_the_first_lease_is_its_own_knob_and_is_never_longer_than_the_idle_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first-lease knob is read off the module and clamped to the idle timeout.

    A first lease longer than the ordinary one is not shorter, so it is clamped; setting the two
    equal, or `0`, asks for the single-lease behaviour.
    """
    monkeypatch.delenv("MCP_SESSION_UNUSED_TIMEOUT_SECONDS", raising=False)
    assert session_unused_timeout(1800.0) == DEFAULT_SESSION_UNUSED_TIMEOUT_SECONDS
    # Clamped: a pod reaping everything at 5 s does not hold an unused session for 60.
    assert session_unused_timeout(5.0) == 5.0
    monkeypatch.setenv("MCP_SESSION_UNUSED_TIMEOUT_SECONDS", "7")
    assert session_unused_timeout(1800.0) == 7.0
    monkeypatch.setenv("MCP_SESSION_UNUSED_TIMEOUT_SECONDS", "0")
    assert session_unused_timeout(1800.0) == 1800.0
    monkeypatch.setenv("MCP_SESSION_UNUSED_TIMEOUT_SECONDS", "not a number")
    assert session_unused_timeout(1800.0) == DEFAULT_SESSION_UNUSED_TIMEOUT_SECONDS


def test_a_handshake_nobody_came_back_for_is_reaped_long_before_a_session_in_use(
    monkeypatch: pytest.MonkeyPatch, serving: Callable[..., Any]
) -> None:
    """A handshake nobody came back for is reaped long before a session in use.

    Without a short first lease, one caller can fill the session ceiling with handshakes and hold it
    for the full idle timeout. Both sessions open the same way and one is spoken to once; that one
    must survive, since upstream extends the deadline on every request.
    """
    monkeypatch.setenv("MCP_SESSION_IDLE_TIMEOUT_SECONDS", str(FULL_LEASE_SECONDS))
    monkeypatch.setenv("MCP_SESSION_UNUSED_TIMEOUT_SECONDS", str(FIRST_LEASE_SECONDS))
    server = _probe_server("first-lease")
    app = connector_app(server, name="first-lease", token_env=TOKEN_ENV)
    with serving(app) as base:
        abandoned = _open_session(base)
        used = _open_session(base)
        assert _ping(base, used).status_code == 200
        time.sleep(FIRST_LEASE_SECONDS * 2.5)
        assert _ping(base, abandoned).status_code == 404, (
            f"a session nobody came back for was still alive {FIRST_LEASE_SECONDS * 2.5:.1f} s "
            f"after its {FIRST_LEASE_SECONDS:.0f} s first lease, so it is holding its slot for the "
            f"full {FULL_LEASE_SECONDS:.0f} s instead"
        )
        assert _ping(base, used).status_code == 200, (
            "a session that had been used was reaped on the first lease; upstream's own push on "
            "every request is what promotes it, and the short lease must not be re-applied over it"
        )


def _no_session_id(base: str, method: str, **kwargs: Any) -> httpx.Response:
    """One request to `/mcp` with no session id — the shape upstream mints on, whatever it is."""
    return httpx.request(
        method,
        f"{base}/mcp",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        },
        timeout=10.0,
        **kwargs,
    )


def test_a_request_the_server_refused_leaves_no_session_behind(
    serving: Callable[..., Any],
) -> None:
    """Upstream mints on the absence of a header, so its own 400s must not leak a session.

    A bare `DELETE`/`GET`, a ping without session id and a malformed body each answer 400 and would
    register an unusable session. Asserted on the instance map, since the status codes were always
    right.
    """
    server = _probe_server("refused")
    app = connector_app(server, name="refused", token_env=TOKEN_ENV)
    with serving(app) as base:
        instances = server.session_manager._server_instances
        refused = [
            _no_session_id(base, "DELETE"),
            _no_session_id(base, "GET"),
            _no_session_id(base, "POST", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            _no_session_id(base, "POST", content=b"{not json"),
        ]
        assert [response.status_code for response in refused] == [400] * 4, (
            f"one of these is no longer a refusal: {[r.status_code for r in refused]}"
        )
        assert instances == {}, (
            f"{len(instances)} session(s) were minted by requests the server itself answered 400; "
            "each one holds a slot on the ceiling and 56.6 kB of the pod until the idle reaper "
            "reaches it"
        )


async def test_the_short_lease_is_never_applied_over_a_session_that_is_already_working() -> None:
    """The short lease is never applied over a session that is already working.

    It is applied after the minting response is written, so a client may already have a tool call
    in flight; overwriting the hold-open deadline would cancel it silently. Driven against the
    helper because the window is microseconds wide: the decision is asserted, not the schedule.
    """
    server = _probe_server("guards")
    server.streamable_http_app()  # what builds the session manager these helpers read
    instances = server.session_manager._server_instances
    working = SimpleNamespace(idle_scope=anyio.CancelScope())
    far = anyio.current_time() + FULL_LEASE_SECONDS
    working.idle_scope.deadline = math.inf
    promoted = SimpleNamespace(idle_scope=anyio.CancelScope(), **{_USED: True})
    promoted.idle_scope.deadline = far
    fresh = SimpleNamespace(idle_scope=anyio.CancelScope())
    fresh.idle_scope.deadline = far
    # Stand-ins: `_start_the_short_lease` reads only `idle_scope` and the used-marker, and a real
    # transport needs a live session. The ignore goes red if upstream's mapping type ever widens.
    instances.update({"working": working, "promoted": promoted, "fresh": fresh})  # type: ignore[dict-item]
    for session_id in ("working", "promoted", "fresh"):
        _start_the_short_lease(server, session_id, unused=FIRST_LEASE_SECONDS)

    assert working.idle_scope.deadline == math.inf, (
        "a tool call in flight had its hold-open deadline replaced by the first lease; the call "
        "would be cancelled from inside the transport with no error written to the caller"
    )
    assert promoted.idle_scope.deadline == far, (
        "a session that had already had a request of its own was put back on the first lease"
    )
    assert fresh.idle_scope.deadline < far, (
        "a just-minted session nobody has spoken to was left on the full idle timeout"
    )
