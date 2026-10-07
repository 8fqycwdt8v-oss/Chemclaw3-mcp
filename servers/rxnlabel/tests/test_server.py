"""The server as Chemclaw3 meets it: a real socket, a real MCP handshake, a real 401.

Runs uvicorn on loopback and talks to it the way the agent will, so a session manager nobody
ran, an unchecked bearer credential, or a manifest that disagrees with the served surface fails
here.
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
from mcp_server_kit.testing import assert_bearer_is_enforced, assert_manifest_matches

TOKEN = "test-token-for-rxnlabel"

BUCHWALD = (
    "Brc1ccccc1.NC1CCCCC1"
    ">CC(C)(C)P(C(C)(C)C)C(C)(C)C.CC(C)(C)[O-].CC#N.CC(=O)O[Pd]OC(C)=O"
    ">c1ccc(NC2CCCCC2)cc1"
)
MANIFEST = Path(__file__).resolve().parents[1] / "connector.yaml"


def _free_port() -> int:
    """An ephemeral loopback port, released immediately for uvicorn to claim."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def running_server() -> Iterator[str]:
    """Run the real app under uvicorn on loopback, and yield its base URL.

    Module-scoped because the server start is the expensive part. The bearer token is set in the
    environment as a deployment sets it, so the auth path under test is the deployed one.
    """
    import os

    os.environ["CHEMCLAW_RXNLABEL_TOKEN"] = TOKEN
    from chemclaw_mcp_rxnlabel.app import app

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
        pytest.fail("the rxnlabel server did not become ready within 30 s")
    yield base
    server.should_exit = True
    thread.join(timeout=10)


def test_healthz_answers_and_names_the_server(running_server: str) -> None:
    """Uvicorn accepts connections only after the lifespan ran, so a 200 here means it did."""
    response = httpx.get(f"{running_server}/healthz", timeout=5.0)
    assert response.status_code == 200
    # "unknown" is the correct revision for a test process; that an image supplies a real one is
    # asserted fleet-wide. `datasets` is present and empty (no corpus); its presence shows a
    # `readiness` callable ran. See `engine/readiness.py`.
    body = response.json()
    # The bounds this pod is running with, so an overlay that moved the batch bound or the
    # admission ceiling is readable from the probe
    # (`D-2026-09-26-a-pod-reports-the-bounds-it-is-running-with`).
    bounds = body.pop("bounds")
    assert bounds["CHEMCLAW_RXNLABEL_MAX_BATCH"] >= 1
    assert bounds["CHEMCLAW_RXNLABEL_MAX_CONCURRENT_BATCHES"] >= 1
    assert body == {
        "status": "ok",
        "server": "rxnlabel",
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
    """The bearer credential is enforced on the mounted MCP surface, driven against the running server.

    `/mcp` is mounted, and a mount bypasses the enclosing app's dependencies, so this cannot be read
    off the source. `assert_bearer_is_enforced` drives every arm (anonymous, wrong token, wrong
    scheme, valid credential, variable unset) once for the whole fleet.
    """
    await assert_bearer_is_enforced(running_server, MANIFEST, token=TOKEN)


@asynccontextmanager
async def _session(base: str) -> AsyncIterator[ClientSession]:
    """An initialised MCP session against the running server, carrying the bearer token.

    The token rides on a caller-supplied httpx client, so the credential is exercised on the real
    path rather than injected past it.
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
        assert "represent_reaction" in names

        result = await session.call_tool(
            "represent_reaction",
            {
                "reaction_smiles": BUCHWALD,
                "species": ["CC(C)(C)P(C(C)(C)C)C(C)(C)C", "CC(C)(C)[O-]"],
            },
        )
        assert result.isError is False
        assert result.structuredContent is not None
        # The two roles the recorded vocabulary cannot express, over a real socket.
        assert [s["role"] for s in result.structuredContent["species"]] == ["ligand", "base"]

        # The manifest is a claim about this surface; here is where the claim is checked against
        # the server that is actually running.
        assert_manifest_matches(MANIFEST, listed.tools)


async def test_a_bad_argument_reaches_the_agent_as_a_usable_message(running_server: str) -> None:
    """A deliberately worded domain error passes through; an internal one would not.

    The batch cap is the one refusal this server issues, and it has to arrive worded: a caller that
    read it as an internal error would retry the same oversized batch until its budget ran out.
    """
    async with _session(running_server) as session:
        result = await session.call_tool(
            "name_reactions",
            {"reactions": [{"id": str(n), "reaction_smiles": BUCHWALD} for n in range(501)]},
        )
        assert result.isError is True
        assert "batch limit" in str(result.content)


async def test_the_version_tool_answers_over_the_wire(running_server: str) -> None:
    """The handshake a caller performs before storing any label from this deployment."""
    async with _session(running_server) as session:
        result = await session.call_tool("labeller_version", {})
        assert result.isError is False
        assert result.structuredContent is not None
        assert result.structuredContent["version"].startswith("rxnlabel@")
        assert "rdkit" in result.structuredContent["components"]
