"""An abandoned MCP session is reaped; one with a call still running is not.

The first half is the leak: `FastMCP` never passes upstream's `session_idle_timeout`, so before
`sessions.py` a session was removed only by an explicit `DELETE`, and a client that vanished left
149 kB and a live anyio task behind for the life of the pod.

The second half is what stops the fix being a worse bug than the leak. Upstream pushes a session's
deadline forward when an HTTP *request* arrives, and a tool call is one request whose SSE body is
written when the work finishes — so on upstream's arithmetic alone a call that outlives the timeout
is cancelled from underneath the caller. `servers/calc` runs CREST searches with a 14,400 s budget
against a timeout this module defaults to 1800 s, so that is not a hypothetical. The counterfactual
test at the bottom drives exactly that: the same slow tool on a server with the timeout set and the
hold-open wrapper absent, which is what upstream's own recommendation would have given this fleet.

Timings are short (1 s timeout, a 2.5 s tool) because the thing under test is a deadline, not a
duration; nothing here sleeps longer than a few seconds.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from mcp_server_kit.app import connector_app
from mcp_server_kit.sessions import (
    DEFAULT_SESSION_IDLE_TIMEOUT_SECONDS,
    session_idle_timeout,
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
    """The leak, and the fix, over a real socket with no client library to tidy up.

    Measured before this: a session id whose client had exited and whose TCP connection was gone
    still answered HTTP 200 ten seconds later, and 500 such sessions on `chem` cost 72.8 MB that
    never came back. The session is alive immediately after the handshake and gone once the idle
    timeout has passed, with nothing in between doing anything at all.
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
    """The half that keeps the fix from cancelling a four-hour calculation as "idle".

    The tool takes 2.5 s against a 1 s idle timeout, so upstream's deadline — armed when the
    `tools/call` request arrived and pushed by nothing afterwards — would have expired 1.5 s before
    the answer existed. It returns, and the session it returned on is still usable.
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
    """The counterfactual: the idle timeout upstream recommends, with nothing holding it open.

    Assembled by hand rather than through `connector_app`, because `connector_app` is the thing
    that installs the hold-open wrapper — this is what following upstream's own recommendation
    would have given a fleet whose flagship tool runs for hours.
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
    """What makes the test above mean something, and it is worse than an error.

    A test that a slow call succeeds proves nothing on its own — it would pass with no idle timeout
    at all. This is the arm that shows the timeout is armed and that the hold-open is what survives
    it: same tool, same 1 s timeout, no wrapper.

    **Measured, the failure is a hang rather than a refusal.** Expiring the session cancels
    `Server.run` and terminates the transport, so the SSE stream the `tools/call` is being answered
    on simply stops; no JSON-RPC error is ever written, and the caller waits until *its* timeout.
    On `servers/calc` that would have been a CREST search dropped at 30 minutes with the chemist
    still holding an open request and nothing in the log but "idle timeout" — which is why the
    hold-open is part of this fix rather than a refinement of it. The `wait_for` here is what keeps
    that hang out of the test suite; without it this test does not fail, it never finishes.
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
    """The arm the first hold-open test could not see: a session carrying *other* traffic.

    Holding the deadline off from inside the tool call is only half of it, because upstream pushes
    the same deadline forward on **every** request for an existing session — a ping and a
    `tools/list` included — which overwrites `math.inf` with `now + timeout` and hands the
    calculation the very cancellation the hold-open exists to prevent. Measured before the
    re-assert, with a 1 s timeout and a 2.5 s tool interfered with at 0.5 s: the call answers in
    **2.51 s** alone, and is cut at **1.51 s** with zero bytes written the moment anything else
    speaks on the session — which is the arithmetic exactly, 0.5 s of interference plus the 1 s
    deadline it re-armed. This docstring shipped saying 5.12 s and ~1.8 s, neither reproducible,
    while `sessions.py` said 1.5 s three lines away: two contradicting figures for one cut, inside
    the commit that fixes the thing they describe.

    The trigger is not hypothetical. Chemclaw3's `core/mcp_session.py` sends a `PingRequest` as a
    cancellation flush when one call of a fan-out times out, so a `calc` session doing exactly what
    it is designed to do destroys the *other* CREST search running beside it.
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
    """The half the idle reaper does not cover: a client that says goodbye properly.

    Upstream's only unconditional removal from `_server_instances` is in `run_server`'s `finally`,
    guarded by `and not http_transport.is_terminated` — and `terminate()` sets that flag as its
    first statement, so a `DELETE` takes exactly the branch that skips the `del`. The entry and its
    transport then stay for the life of the process, and no idle deadline can reach them because
    the scope's task is already gone.

    **This is the fleet's happy path**, which is why it hid: Chemclaw3 sends its `DELETE` from a
    `finally`, and the module's docstring reasoned only about the case where that `finally` is not
    reached. Measured over 300 sessions (`initialize`, `tools/call`, `DELETE`): 300 of 300 stayed
    in the map, every one `is_terminated`, while an abandoned session in the same run was reaped
    correctly. With the sweep, RSS goes flat after the allocator warms — 76 kB/session on the first
    batch of 300 and 81, 40 and 13 bytes on the next three, against a count that climbed by exactly
    300 per batch without it.

    Two sessions rather than one, and one of them never calls a tool: the per-transport wrappers
    are installed from inside `call_tool`, so a session that only initializes and leaves is the
    case a hook on those wrappers would miss.
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
