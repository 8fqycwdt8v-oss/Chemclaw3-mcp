"""The server as Chemclaw3 meets it: a real socket, a real MCP handshake, a real 401.

Runs uvicorn on loopback so a session manager nobody ran, an unchecked bearer credential, or a
manifest that disagrees with the served surface fails here. The bearer check matters most on
this server: it protects an interpreter.
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
from chemclaw_mcp_pyexec import tools
from chemclaw_mcp_pyexec.engine.admission import ADMISSION_MARKER
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp_server_kit.testing import assert_bearer_is_enforced, assert_manifest_matches

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

    `datasets` is present and empty (no corpus); its presence shows a `readiness` callable ran,
    which here proves the child process works. See `engine/readiness.py`.
    """
    response = httpx.get(f"{running_server}/healthz", timeout=5.0)
    assert response.status_code == 200
    body = response.json()
    # The bounds this pod runs with, at the values its environment set, so an overlay that moved the
    # admission ceiling is visible here.
    bounds = body.pop("bounds")
    assert bounds["CHEMCLAW_PYEXEC_MAX_CONCURRENT_RUNS"] >= 1
    assert body == {
        "status": "ok",
        "server": "pyexec",
        "revision": "unknown",
        "datasets": [],
    }


def test_metrics_are_exposed_unauthenticated(running_server: str) -> None:
    """A Prometheus scrape has no identity, and the exposition carries nothing about a request.

    The no-caller/session/argument rule is asserted over the live exposition for every server in
    `packages/mcp_server_kit/tests/test_connector_app.py`.
    """
    response = httpx.get(f"{running_server}/metrics", timeout=5.0)
    assert response.status_code == 200


async def test_the_bearer_credential_is_enforced_on_the_mounted_mcp_surface(
    running_server: str,
) -> None:
    """An anonymous caller must not reach an interpreter. The 401 is the whole control.

    `/mcp` is mounted, and a mount bypasses the enclosing app's dependencies, so this is driven
    against the running server. `assert_bearer_is_enforced` covers every arm (anonymous, wrong
    token, wrong scheme, valid credential, variable unset) once for the whole fleet.
    """
    await assert_bearer_is_enforced(running_server, MANIFEST, token=TOKEN)


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

    That is what lets the agent read the traceback and fix its program; an `isError` result would
    read as "the tool is broken".
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


def test_the_one_tool_is_admission_gated() -> None:
    """The admission gate is on the served callable, as its marker shows.

    Applied under `@server.tool()` so FastMCP registers the guarded function; checked by marker, not
    name, so a later tool added without a gate fails here.
    """
    manager = tools.server._tool_manager
    served = manager.list_tools()
    assert served, "no tools registered"
    for tool in served:
        registered = manager.get_tool(tool.name)
        assert registered is not None
        assert getattr(registered.fn, ADMISSION_MARKER, False), (
            f"{tool.name} is served without an admission slot: it would run whenever a caller "
            "asked, regardless of how many are already running"
        )
