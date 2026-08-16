"""The server as Chemclaw3 meets it: a real socket, a real MCP handshake, a real 401.

The test that matters most for a *fork*. Upstream mounted `fastapi-mcp` over its REST routes and
applied its bearer check as a route `Depends(...)` — and a mount bypasses the enclosing app's
dependencies, so the credential guarded the surface that did not matter. Here the check is ASGI
middleware, and the assertion below is the difference between believing that and knowing it.

Deterministic doubles are registered before the app starts, so the tool call exercises the whole
path — session manager, auth, tool dispatch, the ensemble — without a checkpoint.
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

TOKEN = "test-token-for-rxnpredict"
MANIFEST = Path(__file__).resolve().parents[1] / "connector.yaml"


def _free_port() -> int:
    """An ephemeral loopback port, released immediately for uvicorn to claim."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def running_server() -> Iterator[str]:
    """The real app under uvicorn on loopback, with two forward doubles registered."""
    import os

    os.environ["CHEMCLAW_RXNPREDICT_TOKEN"] = TOKEN
    from chemclaw_mcp_rxnpredict.app import app
    from chemclaw_mcp_rxnpredict.engine import predictors as registry
    from chemclaw_mcp_rxnpredict.engine.base_doubles import FakeForwardPredictor

    saved = dict(registry._FORWARD)
    registry._FORWARD.clear()
    registry.register_forward(FakeForwardPredictor("fake_a", ["CC(=O)Nc1ccccc1", "CCOC(C)=O"]))
    registry.register_forward(FakeForwardPredictor("fake_b", ["CC(=O)Nc1ccccc1"]))

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
        pytest.fail("the rxnpredict server did not become ready within 30 s")
    yield base
    server.should_exit = True
    thread.join(timeout=10)
    registry._FORWARD.clear()
    registry._FORWARD.update(saved)


def test_healthz_answers_and_names_the_server(running_server: str) -> None:
    """Uvicorn accepts connections only after the lifespan ran, so a 200 means it did."""
    response = httpx.get(f"{running_server}/healthz", timeout=5.0)
    assert response.status_code == 200
    # `revision` is part of the probe payload since the handshake started carrying the
    # build (see `mcp_server_kit.app.server_revision`). "unknown" is the correct answer
    # for a test process, which is not built from a Containerfile — that the *image*
    # supplies a real one is asserted in `tests/test_fleet.py`, because a value nothing
    # fills is a provenance record that quietly says nothing.
    assert response.json() == {"status": "ok", "server": "rxnpredict", "revision": "unknown"}


def test_metrics_are_exposed_unauthenticated(running_server: str) -> None:
    """A Prometheus scrape has no identity and the exposition carries counts only."""
    response = httpx.get(f"{running_server}/metrics", timeout=5.0)
    assert response.status_code == 200


def test_the_mcp_surface_refuses_an_unauthenticated_caller(running_server: str) -> None:
    """The assertion this fork exists for: upstream's credential did not cover the mount."""
    response = httpx.post(
        f"{running_server}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"accept": "application/json, text/event-stream"},
        timeout=5.0,
    )
    assert response.status_code == 401


@asynccontextmanager
async def _session(base: str) -> AsyncIterator[ClientSession]:
    """An initialised MCP session carrying the bearer token on the real path."""
    async with (
        httpx.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}) as http_client,
        streamable_http_client(f"{base}/mcp", http_client=http_client) as (rx, tx, _),
        ClientSession(rx, tx) as session,
    ):
        await session.initialize()
        yield session


async def test_a_real_mcp_session_lists_and_calls_a_tool(running_server: str) -> None:
    """The handshake plus an ensemble prediction — the shape of every turn through this server."""
    async with _session(running_server) as session:
        listed = await session.list_tools()
        names = sorted(tool.name for tool in listed.tools)
        assert "predict_forward_reaction" in names

        result = await session.call_tool(
            "predict_forward_reaction", {"reactants": "CC(=O)Cl.Nc1ccccc1", "top_k": 2}
        )
        assert result.isError is False
        assert result.structuredContent is not None
        consensus = result.structuredContent["consensus"]
        assert consensus[0]["product_smiles"] == "CC(=O)Nc1ccccc1"
        assert consensus[0]["vote_count"] == 2

        # The manifest is a claim about this surface; this is where the claim is checked against
        # the server that is actually running.
        assert_manifest_matches(MANIFEST, names)


async def test_an_unknown_model_reaches_the_agent_as_a_usable_message(running_server: str) -> None:
    """A deliberately worded domain error passes through; an internal one would be replaced."""
    async with _session(running_server) as session:
        result = await session.call_tool(
            "predict_forward_single_model", {"model_name": "nope", "reactants": "CCO"}
        )
        assert result.isError is True
        assert "fake_a" in str(result.content)
