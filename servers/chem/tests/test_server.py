"""The server as Chemclaw3 meets it: a real socket, a real MCP handshake, a real 401.

This tests the deployment surface: the session manager runs, the bearer credential is checked,
and the manifest matches the served tools. Specific to `chem`: an unknown name in
`resolve_compound` must arrive as a worded result, not an error or empty content, or the agent
reads a miss as a broken tool.
"""

from __future__ import annotations

import json
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
from mcp_server_kit.testing import assert_bearer_is_enforced, assert_manifest_matches

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

    Module-scoped because a server start is expensive. The bearer token is set in the environment as
    a deployment sets it.
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
    # `revision` is "unknown" in a test process, which is not built from a Containerfile; the fleet
    # tests assert the image supplies a real one.
    from chemclaw_mcp_chem.engine import reagents

    corpus = reagents.dataset()
    body = response.json()
    assert body["status"] == "ok"
    assert body["server"] == "chem"
    assert body["revision"] == "unknown"
    # `/healthz` names the corpora this pod actually verified, so a failed checksum is unready and
    # an operator can see which table a pod serves.
    assert body["datasets"] == [f"{corpus.name}@{corpus.version}"]


def test_metrics_are_exposed_unauthenticated(running_server: str) -> None:
    """A Prometheus scrape has no identity, and the exposition carries nothing about a request.

    What it must never publish (caller, session, correlation id, tool argument) is asserted over the
    live exposition in `packages/mcp_server_kit/tests/test_connector_app.py`.
    """
    response = httpx.get(f"{running_server}/metrics", timeout=5.0)
    assert response.status_code == 200


async def test_the_bearer_credential_is_enforced_on_the_mounted_mcp_surface(
    running_server: str,
) -> None:
    """The bearer credential is enforced on the mounted `/mcp` surface.

    Driven against the running server, because a mount bypasses the enclosing app's dependencies.
    `mcp_server_kit.testing.assert_bearer_is_enforced` holds every arm once for all servers.
    """
    await assert_bearer_is_enforced(running_server, MANIFEST, token=TOKEN)


@asynccontextmanager
async def _session(base: str) -> AsyncIterator[ClientSession]:
    """An initialised MCP session against the running server, carrying the bearer token.

    The token rides on a caller-supplied httpx client, so the credential is exercised on the real
    path.
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
        assert_manifest_matches(MANIFEST, listed.tools)


async def test_an_unknown_name_comes_back_as_a_result_not_an_error(running_server: str) -> None:
    """A miss is an answer. If it arrived as an error the agent would guess a structure instead."""
    async with _session(running_server) as session:
        result = await session.call_tool("resolve_compound", {"name": "unobtainium"})
        assert result.isError is False
        assert result.structuredContent == {"result": None}


async def test_an_unknown_name_is_said_in_words_on_the_wire(running_server: str) -> None:
    """An unknown name is said in words on the wire.

    FastMCP writes a `None` return as zero content blocks, so the miss must be a text block.
    """
    async with _session(running_server) as session:
        result = await session.call_tool("resolve_compound", {"name": "aniline"})
        assert result.isError is False
        text = "".join(getattr(block, "text", "") for block in result.content)
        assert text.strip(), "an unrecognised name came back with no text at all"
        said = json.loads(text)
        assert said["recognised"] is False
        assert said["query"] == "aniline"
        assert "SMILES" in said["accepts"]


async def test_the_declared_output_schema_is_unchanged(running_server: str) -> None:
    """Chemclaw3 was told this tool returns `ResolvedCompound | null`; the miss's words must not
    widen that. Building the result by hand is what could, so the declaration is pinned here.
    """
    async with _session(running_server) as session:
        listed = await session.list_tools()
        (tool,) = [tool for tool in listed.tools if tool.name == "resolve_compound"]
        assert tool.outputSchema is not None
        assert tool.outputSchema["required"] == ["result"]
        branches = tool.outputSchema["properties"]["result"]["anyOf"]
        assert {"type": "null"} in branches
        assert len(branches) == 2
        assert set(tool.outputSchema["$defs"]) == {"ResolvedCompound"}


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

    `InvalidSmilesError` is a `ValueError` so the chemist is told which string was refused.
    """
    async with _session(running_server) as session:
        result = await session.call_tool("render_structure", {"smiles": "CCO junk"})
        assert result.isError is True
        assert "invalid SMILES" in str(result.content)
