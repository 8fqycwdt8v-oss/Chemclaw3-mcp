"""The server as Chemclaw3 meets it: a real socket, a real MCP handshake, a real 401.

The rest of this directory tests functions. This tests the *deployment surface*, and it is the test
that would catch each of the three defects Chemclaw3 recorded on this exact seam: a mounted MCP app
whose session manager nobody ran, a bearer credential the serving side never checked, and a manifest
claiming a tool surface the server did not have.

The bearer check matters more here than anywhere else in this fleet. Every other server refuses an
anonymous caller to protect a table; this one refuses to protect an interpreter.
"""

from __future__ import annotations

import os
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

TOKEN = "test-token-for-pyexec"
MANIFEST = Path(__file__).resolve().parents[1] / "connector.yaml"


def _free_port() -> int:
    """An ephemeral loopback port, released immediately for uvicorn to claim."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def running_server() -> Iterator[str]:
    """Run the real app under uvicorn on loopback, and yield its base URL."""
    os.environ["CHEMCLAW_PYEXEC_TOKEN"] = TOKEN
    from chemclaw_mcp_pyexec.app import app

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
    else:  # pragma: no cover — only reached if the app never becomes ready.
        pytest.fail("the pyexec server did not become ready within 30 s")
    yield base
    server.should_exit = True
    thread.join(timeout=10)


def test_healthz_answers_and_names_the_server(running_server: str) -> None:
    """Uvicorn accepts connections only after the lifespan ran, so a 200 here means it did.

    `datasets` is present and empty because this server vendors no corpus. Its presence is the
    assertion: it is what `connector_app` adds only when a `readiness` callable actually ran, and
    without one this route was a constant 200 that proved nothing about the child process every
    call here depends on. See `engine/readiness.py`.
    """
    response = httpx.get(f"{running_server}/healthz", timeout=5.0)
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "server": "pyexec",
        "revision": "unknown",
        "datasets": [],
    }


def test_metrics_are_exposed_unauthenticated(running_server: str) -> None:
    """A Prometheus scrape has no identity, and the exposition carries nothing about a request.

    Not "counts only": the default registry publishes `python_info` and the `process_*`
    collectors. What an unauthenticated endpoint must never publish is a caller, a session, a
    correlation id or a tool argument — asserted over the live exposition in
    `packages/mcp_server_kit/tests/test_connector_app.py`, for every server at once.
    """
    response = httpx.get(f"{running_server}/metrics", timeout=5.0)
    assert response.status_code == 200


def test_the_mcp_surface_refuses_an_unauthenticated_caller(running_server: str) -> None:
    """An anonymous caller must not reach an interpreter. The 401 is the whole control."""
    response = httpx.post(
        f"{running_server}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"accept": "application/json, text/event-stream"},
        timeout=5.0,
    )
    assert response.status_code == 401


@asynccontextmanager
async def _session(base: str) -> AsyncIterator[ClientSession]:
    """An initialised MCP session against the running server, carrying the bearer token."""
    async with (
        httpx.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}) as http_client,
        streamable_http_client(f"{base}/mcp", http_client=http_client) as (rx, tx, _),
        ClientSession(rx, tx) as session,
    ):
        await session.initialize()
        yield session


async def test_a_real_mcp_session_lists_and_runs_an_analysis(running_server: str) -> None:
    """The handshake plus a tool call — the shape of every turn Chemclaw3 will run through here."""
    async with _session(running_server) as session:
        listed = await session.list_tools()
        names = sorted(tool.name for tool in listed.tools)
        assert names == ["run_python"]

        result = await session.call_tool(
            "run_python",
            {"code": "result = sum(data['xs'])", "data": {"xs": [1, 2, 3]}},
        )
        assert result.isError is False
        assert result.structuredContent is not None
        assert result.structuredContent["ok"] is True
        assert result.structuredContent["result"] == 6
        assert "pyexec sandbox" in result.structuredContent["source"]

        # The manifest is a claim about this surface; here is where it is checked against the
        # server that is actually running.
        assert_manifest_matches(MANIFEST, listed.tools)


async def test_a_failing_program_returns_a_result_rather_than_an_error(running_server: str) -> None:
    """A caller's bug is a normal answer carrying a traceback, not a tool failure.

    The distinction is what lets the agent read the traceback and fix its program. An `isError`
    result would reach it as "the tool is broken", which is a different thing to do about it — and
    it is the shape `D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed` warns about from
    the other side.
    """
    async with _session(running_server) as session:
        result = await session.call_tool("run_python", {"code": "result = 1 / 0"})
        assert result.isError is False
        assert result.structuredContent is not None
        assert result.structuredContent["ok"] is False
        assert "ZeroDivisionError" in result.structuredContent["error"]


async def test_the_sandbox_holds_over_the_wire(running_server: str) -> None:
    """The refusals are properties of the served tool, not only of the engine under a unit test."""
    async with _session(running_server) as session:
        result = await session.call_tool("run_python", {"code": "import os\nresult = os.getcwd()"})
        assert result.structuredContent is not None
        assert result.structuredContent["ok"] is False
        assert "not available in the analysis sandbox" in result.structuredContent["error"]
