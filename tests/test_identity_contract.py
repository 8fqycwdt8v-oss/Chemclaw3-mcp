"""The `X-Chemclaw-*` header contract with Chemclaw3, checked against the names it actually sends.

Header lookup is case-insensitive but not suffix-insensitive, so a constant that differs from the
sent name binds an empty value, which `ContextFilter` writes onto every log line as "no
correlation", and a durable record stamped with it would carry nothing.

The literals are transcribed from `chemclaw.connectors.identity.STAMPED_HEADERS`, not imported
from `mcp_server_kit.identity`: a test against this repository's own constants only proves it
agrees with itself. Change one only because Chemclaw3 changed it.

Driven through a real `connector_app` over a real MCP session, because the caller is bound twice
(middleware in the ASGI task, `_bind_caller_per_tool_call` in the session manager's) and only the
second is what a tool body reads.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator

import anyio
import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP
from mcp_server_kit import connector_app, current_caller

# Transcribed from `chemclaw.connectors.identity`. See the module docstring for why these are
# literals rather than imports.
SENT_ACTOR = "X-Chemclaw-Actor"
SENT_SESSION = "X-Chemclaw-Session"
SENT_CORRELATION = "X-Chemclaw-Correlation-Id"
SENT_DRY_RUN = "X-Chemclaw-Dry-Run"

# Chemclaw3's `STAMPED_HEADERS`, in its order. Its own comment says a new `X-Chemclaw-*` header
# belongs in that tuple the day it is written, so transcribing the whole tuple rather than four
# loose names is what makes a fifth one a visible gap here rather than a silent one.
SENT_STAMPED = (SENT_ACTOR, SENT_SESSION, SENT_CORRELATION, SENT_DRY_RUN)

ACTOR = "alice-oid"
SESSION = "sess-1"
CORRELATION = "corr-abc"


#: What each `slow_whoami` body read, appended from the server thread's loop — the concurrent
#: test's evidence, since two raw POSTs' SSE bodies are harder to parse than one shared list.
SEEN_CONCURRENT: list[tuple[str, str, str]] = []


def _probe_app() -> uvicorn.Config:
    """A `connector_app` whose tools report the caller their own bodies can see."""
    server = FastMCP("identity-probe")

    @server.tool()
    def whoami() -> dict[str, str]:
        """Report the caller bound for this tool call."""
        caller = current_caller()
        return {
            "actor": caller.actor,
            "session": caller.session,
            "correlation": caller.correlation,
        }

    @server.tool()
    async def slow_whoami() -> str:
        """Dawdle long enough that two calls overlap, then record the caller this body sees."""
        await anyio.sleep(0.25)
        caller = current_caller()
        SEEN_CONCURRENT.append((caller.actor, caller.session, caller.correlation))
        return "ok"

    app = connector_app(server, name="identity-probe", token_env=None)
    return uvicorn.Config(app, host="127.0.0.1", port=_free_port(), log_level="warning")


def _free_port() -> int:
    """An ephemeral loopback port, released immediately for uvicorn to claim."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def probe_url() -> Iterator[str]:
    """Run the probe server under uvicorn on loopback and yield its MCP endpoint.

    A real socket rather than an ASGI transport: the DNS-rebinding guard refuses a synthetic `Host`,
    and driving the session manager's lifespan from the test's task would exit an `anyio` cancel
    scope in the wrong task.
    """
    config = _probe_app()
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://{config.host}:{config.port}"
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/healthz", timeout=1.0).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:  # pragma: no cover - the server failed to come up at all
        raise RuntimeError("the identity probe server did not start")
    yield f"{base}/mcp"
    server.should_exit = True


async def _call_whoami(url: str, headers: dict[str, str]) -> dict[str, str]:
    """Open a real MCP session with `headers` and return what the tool body read."""
    async with (
        httpx.AsyncClient(headers=headers) as client,
        streamable_http_client(url, http_client=client) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool("whoami", {})
        assert not result.isError
        payload: dict[str, str] = json.loads(getattr(result.content[0], "text", "{}"))
        return payload


async def test_a_tool_body_reads_every_header_chemclaw3_sends(probe_url: str) -> None:
    """All three identity values reach the tool body under the sender's own spellings.

    The regression this pins: `correlation` came back `""` here while the other two were fine, so
    any test that checked "identity arrives" without naming each field would have passed.
    """
    seen = await _call_whoami(
        probe_url,
        {
            SENT_ACTOR: ACTOR,
            SENT_SESSION: SESSION,
            SENT_CORRELATION: CORRELATION,
            SENT_DRY_RUN: "false",
        },
    )
    assert seen == {"actor": ACTOR, "session": SESSION, "correlation": CORRELATION}


async def test_an_absent_header_is_empty_rather_than_missing(probe_url: str) -> None:
    """An absent header binds as empty, not as an error.

    Chemclaw3 omits a header off the request path rather than sending an empty one, so the absent
    case is real and must be typed like the present one.
    """
    seen = await _call_whoami(probe_url, {})
    assert seen == {"actor": "", "session": "", "correlation": ""}


@pytest.mark.parametrize(
    ("constant", "sent"),
    [
        ("HEADER_ACTOR", SENT_ACTOR),
        ("HEADER_SESSION", SENT_SESSION),
        ("HEADER_CORRELATION", SENT_CORRELATION),
        ("HEADER_DRY_RUN", SENT_DRY_RUN),
    ],
)
def test_each_constant_names_the_header_that_is_sent(constant: str, sent: str) -> None:
    """Each constant names the header that is sent, case aside.

    Names which constant drifted. Kept beside the end-to-end check: a constant can be right while
    the binding is not, and `HEADER_DRY_RUN` has no binding (it is read directly in
    `CallerLogMiddleware`), so only this covers it.
    """
    from mcp_server_kit import identity

    assert getattr(identity, constant) == sent.lower()


async def test_two_concurrent_calls_on_one_session_each_read_their_own_caller(
    probe_url: str,
) -> None:
    """Two concurrent calls on one MCP session each read their own caller.

    Chemclaw3 gathers a whole tool batch, so calls overlap on one `mcp-session-id`; interleaved
    bind/reset pairs would stamp one call with another's identity. Raw JSON-RPC posts, since
    `ClientSession` fixes headers at construction and the subject is per-call identity.
    """

    def who(name: str) -> dict[str, str]:
        return {
            SENT_ACTOR: f"{name}-oid",
            SENT_SESSION: f"sess-{name}",
            SENT_CORRELATION: f"corr-{name}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }

    SEEN_CONCURRENT.clear()
    async with httpx.AsyncClient(timeout=10.0) as client:
        opened = await client.post(
            probe_url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "probe", "version": "1"},
                },
            },
            headers=who("opener"),
        )
        session_id = opened.headers["mcp-session-id"]
        await client.post(
            probe_url,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={**who("opener"), "mcp-session-id": session_id},
        )

        async def call(name: str, request_id: int) -> None:
            await client.post(
                probe_url,
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {"name": "slow_whoami", "arguments": {}},
                },
                headers={**who(name), "mcp-session-id": session_id},
            )

        async with anyio.create_task_group() as group:
            group.start_soon(call, "alice", 2)
            group.start_soon(call, "bob", 3)

    assert sorted(SEEN_CONCURRENT) == [
        ("alice-oid", "sess-alice", "corr-alice"),
        ("bob-oid", "sess-bob", "corr-bob"),
    ], (
        f"two concurrent calls on one session read {SEEN_CONCURRENT}; the per-call binding "
        "interleaved and a stamped record would carry the other caller's identity"
    )


def test_the_constants_are_exactly_the_headers_chemclaw3_stamps() -> None:
    """The constants are exactly the headers Chemclaw3 stamps, as a set.

    Otherwise a header Chemclaw3 adds is invisible here and one it deletes lives on.
    """
    from mcp_server_kit import identity

    ours = {
        identity.HEADER_ACTOR,
        identity.HEADER_SESSION,
        identity.HEADER_CORRELATION,
        identity.HEADER_DRY_RUN,
    }
    assert ours == {sent.lower() for sent in SENT_STAMPED}
