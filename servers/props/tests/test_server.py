"""The server as Chemclaw3 meets it: a real socket, a real MCP handshake, a real 401.

Everything else in this directory tests functions. This tests the *deployment surface* — and it is
the test that would have caught each of the three defects Chemclaw3 recorded on this exact seam:

- a mounted MCP app whose session manager nobody ran (accepts the connection, hangs on the call);
- a bearer credential the serving side never checked;
- a manifest that claimed a tool surface the server did not have.

So it runs uvicorn on a loopback port and talks to it the way the agent will.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp_server_kit.testing import assert_manifest_matches

TOKEN = "test-token-for-props"
MANIFEST = Path(__file__).resolve().parents[1] / "connector.yaml"


def _free_port() -> int:
    """An ephemeral loopback port, released immediately for uvicorn to claim."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def running_server() -> Iterator[str]:
    """Run the real app under uvicorn on loopback, and yield its base URL.

    Module-scoped because a server start is the expensive part of this file, and every test here
    wants the same one. The bearer token is set in the environment the same way a deployment sets
    it, so the auth path under test is the deployed one rather than a stub.
    """
    import os

    os.environ["CHEMCLAW_PROPS_TOKEN"] = TOKEN
    from chemclaw_mcp_props.app import app

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/healthz", timeout=1.0).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:  # pragma: no cover - only reached if the app never becomes ready
        pytest.fail("the props server did not become ready within 30 s")
    yield base
    server.should_exit = True
    thread.join(timeout=10)


def test_healthz_answers_and_names_the_server(running_server: str) -> None:
    """Uvicorn accepts connections only after the lifespan ran, so a 200 here means it did."""
    response = httpx.get(f"{running_server}/healthz", timeout=5.0)
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "server": "props"}


def test_metrics_are_exposed_unauthenticated(running_server: str) -> None:
    """A Prometheus scrape has no identity and the exposition carries counts only."""
    response = httpx.get(f"{running_server}/metrics", timeout=5.0)
    assert response.status_code == 200
    assert "python_info" in response.text


def test_the_mcp_surface_refuses_an_unauthenticated_caller(running_server: str) -> None:
    """The refusal Chemclaw3's connector fleet did not have until an unauthenticated handshake
    completed against it."""
    response = httpx.post(
        f"{running_server}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"accept": "application/json, text/event-stream"},
        timeout=5.0,
    )
    assert response.status_code == 401


@asynccontextmanager
async def _session(base: str) -> AsyncIterator[ClientSession]:
    """An initialised MCP session against the running server, carrying the bearer token.

    The token rides on a caller-supplied httpx client because that is how this version of the MCP
    client takes headers — which also means the credential is exercised on the real path rather
    than injected past it.
    """
    async with (
        httpx.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}) as http_client,
        streamable_http_client(f"{base}/mcp", http_client=http_client) as (rx, tx, _),
        ClientSession(rx, tx) as session,
    ):
        await session.initialize()
        yield session


async def test_a_real_mcp_session_lists_and_calls_a_tool(running_server: str) -> None:
    """The handshake plus a tool call — the shape of every turn Chemclaw3 will run through here."""
    async with _session(running_server) as session:
        listed = await session.list_tools()
        names = sorted(tool.name for tool in listed.tools)
        assert "solvent_properties" in names

        result = await session.call_tool("solvent_properties", {"name": "2-MeTHF"})
        assert result.isError is False
        assert result.structuredContent is not None
        assert result.structuredContent["boiling_point_c"] == 80.2

        # The manifest is a claim about this surface; here is where the claim is checked against
        # the server that is actually running.
        assert_manifest_matches(MANIFEST, names)


async def test_an_oversized_chunked_body_is_refused_by_the_real_mount(running_server: str) -> None:
    """413 on the mounted transport, over a real socket — measured, because it was not true.

    The kit's unit test drives a middleware stack it assembles itself. This drives the one
    `connector_app` assembles, through uvicorn and into the mounted MCP transport: the configuration
    where an oversized chunked body used to leave an unhandled `ExceptionGroup` and send nothing at
    all.

    The session id is not decoration. Without one the transport rejects the POST on sight and never
    reads the body, so the cap is never the thing that refuses and the test would pass while proving
    nothing. With one, the transport is reading — which is the case that has to end in a 413.
    """
    async with (
        httpx.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}) as http_client,
        streamable_http_client(f"{running_server}/mcp", http_client=http_client) as (rx, tx, ids),
        ClientSession(rx, tx) as session,
    ):
        await session.initialize()
        session_id = ids()
        assert session_id, "this transport did not issue a session id; the test cannot be faithful"

        async def oversized() -> AsyncIterator[bytes]:
            for _ in range(64):
                yield b"x" * 32_768  # 2 MB, over the 1 MB default, and no content-length

        response = await http_client.post(
            f"{running_server}/mcp",
            content=oversized(),
            headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
                "mcp-session-id": session_id,
            },
            timeout=30.0,
        )
    assert response.status_code == 413


async def test_a_bad_argument_reaches_the_agent_as_a_usable_message(running_server: str) -> None:
    """A deliberately worded domain error passes through; an internal one would not."""
    async with _session(running_server) as session:
        result = await session.call_tool("solvent_properties", {"name": "unobtainium"})
        assert result.isError is True
        assert "vendored solvent table" in str(result.content)


async def test_a_misspelled_tool_name_says_so(running_server: str) -> None:
    """An unknown tool is the one error an agent can recover from, and it was being suppressed.

    The sanitiser replaced it because it carries no `__cause__`, so a misspelled tool — and a
    manifest advertising a tool this server no longer has, which is the drift the fleet tests exist
    for — both arrived as "an internal error occurred".
    """
    async with _session(running_server) as session:
        result = await session.call_tool("solvent_propertys", {"name": "toluene"})
        assert result.isError is True
        assert "solvent_propertys" in str(result.content)
        assert "internal error" not in str(result.content)
