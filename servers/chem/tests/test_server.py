"""The server as Chemclaw3 meets it: a real socket, a real MCP handshake, a real 401.

Everything else in this directory tests functions. This tests the *deployment surface* — and it is
the test that would have caught each of the three defects Chemclaw3 recorded on this exact seam:

- a mounted MCP app whose session manager nobody ran (accepts the connection, hangs on the call);
- a bearer credential the serving side never checked;
- a manifest that claimed a tool surface the server did not have.

So it runs uvicorn on a loopback port and talks to it the way the agent will. One thing here is
specific to `chem` and worth the extra test: `resolve_compound` returns `None` for an unknown name,
and "the tool answered, and the answer is nothing" has to survive the wire as a *result* rather
than as an error, or the agent will read a miss as a broken tool and start guessing structures.
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

TOKEN = "test-token-for-chem"
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

    os.environ["CHEMCLAW_CHEM_TOKEN"] = TOKEN
    from chemclaw_mcp_chem.app import app

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
        pytest.fail("the chem server did not become ready within 30 s")
    yield base
    server.should_exit = True
    thread.join(timeout=10)


def test_healthz_answers_and_names_the_server(running_server: str) -> None:
    """Uvicorn accepts connections only after the lifespan ran, so a 200 here means it did."""
    response = httpx.get(f"{running_server}/healthz", timeout=5.0)
    assert response.status_code == 200
    # `revision` is part of the probe payload since the handshake started carrying the
    # build (see `mcp_server_kit.app.server_revision`). "unknown" is the correct answer
    # for a test process, which is not built from a Containerfile — that the *image*
    # supplies a real one is asserted in `tests/test_fleet.py`, because a value nothing
    # fills is a provenance record that quietly says nothing.
    assert response.json() == {"status": "ok", "server": "chem", "revision": "unknown"}


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
    """Chemclaw3's own `chem` bundle declares `auth: {mode: none}`; across a network it cannot."""
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
        assert "resolve_compound" in names

        result = await session.call_tool("resolve_compound", {"name": "2-MeTHF"})
        assert result.isError is False
        assert result.structuredContent is not None
        assert result.structuredContent["result"]["smiles"] == "CC1CCCO1"

        # The manifest is a claim about this surface; here is where the claim is checked against
        # the server that is actually running.
        assert_manifest_matches(MANIFEST, names)


async def test_an_unknown_name_comes_back_as_a_result_not_an_error(running_server: str) -> None:
    """A miss is an answer. If it arrived as an error the agent would guess a structure instead."""
    async with _session(running_server) as session:
        result = await session.call_tool("resolve_compound", {"name": "unobtainium"})
        assert result.isError is False
        assert result.structuredContent == {"result": None}


async def test_a_charge_table_survives_the_wire(running_server: str) -> None:
    """The one tool with a nested structured result, and the one whose numbers a chemist weighs."""
    async with _session(running_server) as session:
        result = await session.call_tool(
            "stoichiometry_table",
            {
                "basis": "AcOH",
                "basis_mass_g": 100.0,
                "reagents": ["TEA"],
                "equivalents": [1.2],
                "solvents": ["THF"],
                "volumes": [10.0],
            },
        )
        assert result.isError is False
        assert result.structuredContent is not None
        rows = result.structuredContent["rows"]
        assert [row["role"] for row in rows] == ["basis", "reagent", "solvent"]
        assert rows[2]["volume_ml"] == 1000.0


async def test_a_bad_argument_reaches_the_agent_as_a_usable_message(running_server: str) -> None:
    """A deliberately worded domain error passes through; an internal one would not.

    `InvalidSmilesError` is a `ValueError` for exactly this reason — `connector_app` replaces every
    other exception with a generic notice, and a chemist who typed a malformed structure needs to
    be told which string was refused.
    """
    async with _session(running_server) as session:
        result = await session.call_tool("render_structure", {"smiles": "CCO junk"})
        assert result.isError is True
        assert "invalid SMILES" in str(result.content)
