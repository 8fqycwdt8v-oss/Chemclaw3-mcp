"""The server as Chemclaw3 meets it: a real socket, a real MCP handshake, a real 401.

Runs uvicorn on loopback so a session manager nobody ran, an unchecked bearer credential, or a
manifest that disagrees with the served surface fails here. Specific to `chembl`: the attribution is
a structured field and must survive serialization to reach the model, and a refused SMILES must
arrive as a readable message rather than an internal error.
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

TOKEN = "test-token-for-chembl"
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

    os.environ["CHEMCLAW_CHEMBL_TOKEN"] = TOKEN
    from chemclaw_mcp_chembl.app import app

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
        pytest.fail("the chembl server did not become ready within 30 s")
    yield base
    server.should_exit = True
    thread.join(timeout=10)


def test_healthz_answers_and_names_the_server(running_server: str) -> None:
    """Uvicorn accepts connections only after the lifespan ran, so a 200 here means it did."""
    response = httpx.get(f"{running_server}/healthz", timeout=5.0)
    assert response.status_code == 200
    from chemclaw_mcp_chembl.engine.readiness import verified_corpora

    body = response.json()
    assert body["status"] == "ok"
    assert body["server"] == "chembl"
    assert body["revision"] == "unknown"
    # The corpus this pod verified, by name and version: `chembl-subset@37-fixture` or `-full`, so
    # an operator can tell from a scrape which tier a pod is serving.
    assert body["datasets"] == [f"{corpus.name}@{corpus.version}" for corpus in verified_corpora()]
    assert body["datasets"][0].startswith("chembl-subset@37-")


def test_metrics_are_exposed_unauthenticated(running_server: str) -> None:
    """A Prometheus scrape has no identity, and the exposition carries nothing about a request."""
    response = httpx.get(f"{running_server}/metrics", timeout=5.0)
    assert response.status_code == 200


async def test_the_bearer_credential_is_enforced_on_the_mounted_mcp_surface(
    running_server: str,
) -> None:
    """The bearer credential is enforced on the mounted MCP surface, against the running server.

    `/mcp` is mounted, and a mount bypasses the enclosing app's dependencies, so this cannot be read
    off the source. `assert_bearer_is_enforced` drives every arm (anonymous, wrong token, wrong
    scheme, valid credential, variable unset) once for the whole fleet.
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


async def test_a_real_mcp_session_lists_and_calls_both_tools(running_server: str) -> None:
    """The handshake plus a call of each tool, the manifest checked against what is served."""
    async with _session(running_server) as session:
        listed = await session.list_tools()
        assert sorted(tool.name for tool in listed.tools) == [
            "chembl_bioactivity_summary",
            "chembl_lookup_compound",
        ]
        assert_manifest_matches(MANIFEST, listed.tools)

        found = await session.call_tool("chembl_lookup_compound", {"identifier": "CHEMBL25"})
        assert found.isError is False
        assert found.structuredContent is not None
        assert found.structuredContent["compounds"][0]["pref_name"] == "ASPIRIN"

        summary = await session.call_tool(
            "chembl_bioactivity_summary", {"identifier": "CHEMBL25", "limit": 3}
        )
        assert summary.isError is False
        assert summary.structuredContent is not None
        assert summary.structuredContent["chembl_id"] == "CHEMBL25"
        assert 0 < len(summary.structuredContent["rows"]) <= 3


async def test_the_attribution_arrives_on_the_wire_as_a_structured_field(
    running_server: str,
) -> None:
    """A field of the answer, not a log line: only the wire shows what the model reads."""
    async with _session(running_server) as session:
        for tool, arguments in (
            ("chembl_lookup_compound", {"identifier": "aspirin"}),
            ("chembl_bioactivity_summary", {"identifier": "aspirin"}),
        ):
            result = await session.call_tool(tool, arguments)
            assert result.isError is False and result.structuredContent is not None
            notice = result.structuredContent["attribution"]
            assert notice["licence"] == "CC BY-SA 3.0"
            assert notice["source"] == "ChEMBL release 37"
            assert "subset + aggregation" in notice["changes"]
            assert notice["source_url"].startswith("https://ftp.ebi.ac.uk/")


async def test_a_miss_comes_back_as_a_result_not_an_error(running_server: str) -> None:
    """A compound outside the subset is a result, so the agent does not invent one."""
    async with _session(running_server) as session:
        result = await session.call_tool(
            "chembl_lookup_compound", {"identifier": "CHEMBL999999999"}
        )
        assert result.isError is False
        assert result.structuredContent is not None
        assert result.structuredContent["found"] is False
        assert result.structuredContent["attribution"]["licence"] == "CC BY-SA 3.0"


async def test_a_refused_structure_reaches_the_agent_as_a_usable_message(
    running_server: str,
) -> None:
    """RDKit reads `"CCO junk"` as ethanol; the alternative to a refusal is the wrong molecule."""
    async with _session(running_server) as session:
        result = await session.call_tool(
            "chembl_lookup_compound", {"identifier": "CCO junk", "kind": "smiles"}
        )
        assert result.isError is True
        assert "invalid SMILES" in str(result.content)
