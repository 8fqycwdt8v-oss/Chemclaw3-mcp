"""The server as Chemclaw3 meets it: a real socket, a real MCP handshake, a real 401.

Runs uvicorn on loopback so a session manager nobody ran, an unchecked bearer credential, or a
manifest that disagrees with the served surface fails here. Specific to `safety`: the verdict
(a `computed_field`) must survive serialization to reach the model, and a refused SMILES must
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

TOKEN = "test-token-for-safety"
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

    os.environ["CHEMCLAW_SAFETY_TOKEN"] = TOKEN
    from chemclaw_mcp_safety.app import app

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
        pytest.fail("the safety server did not become ready within 30 s")
    yield base
    server.should_exit = True
    thread.join(timeout=10)


def test_healthz_answers_and_names_the_server(running_server: str) -> None:
    """Uvicorn accepts connections only after the lifespan ran, so a 200 here means it did."""
    response = httpx.get(f"{running_server}/healthz", timeout=5.0)
    assert response.status_code == 200
    # "unknown" is the correct revision for a test process; that an image supplies a real one is
    # asserted fleet-wide.
    from chemclaw_mcp_safety.engine.readiness import verified_corpora

    body = response.json()
    assert body["status"] == "ok"
    assert body["server"] == "safety"
    assert body["revision"] == "unknown"
    # The corpora this pod verified, by name and version, so `/healthz` reports whether the server
    # can answer and an operator can confirm which tables it serves.
    assert body["datasets"] == [f"{corpus.name}@{corpus.version}" for corpus in verified_corpora()]


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
    """The bearer credential is enforced on the mounted MCP surface, against the running server.

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
        assert "screen_hazards" in names

        result = await session.call_tool("screen_hazards", {"smiles": ["CCCN=[N+]=[N-]"]})
        assert result.isError is False
        assert result.structuredContent is not None
        flags = result.structuredContent["flags"]
        assert [flag["rule_id"] for flag in flags] == ["organic-azide"]
        assert result.structuredContent["screened"] == ["CCCN=[N+]=[N-]"]

        # The manifest is a claim about this surface; here is where the claim is checked against the
        # server that is actually running.
        assert_manifest_matches(MANIFEST, listed.tools)


async def test_a_clean_screen_arrives_carrying_its_disclaimer(running_server: str) -> None:
    """A clean screen arrives on the wire carrying its disclaimer: "no match" is not "safe".

    A plain property is not serialized; only the wire shows whether the `computed_field` reached
    the payload the model reads.
    """
    async with _session(running_server) as session:
        result = await session.call_tool("screen_hazards", {"smiles": ["CCO"]})
        assert result.isError is False
        assert result.structuredContent is not None
        verdict = result.structuredContent["verdict"]
        assert result.structuredContent["flags"] == []
        assert "not a safety assessment" in verdict


async def test_a_clean_genotoxicity_screen_says_what_it_is_not(running_server: str) -> None:
    """A clean genotoxicity screen says on the wire what it is not.

    An empty alert list reads as "not mutagenic"; the payload names individually the four things
    this system cannot produce, since a generic "expert assessment required" was not enough.
    """
    async with _session(running_server) as session:
        result = await session.call_tool("screen_genotoxic_alerts", {"smiles": ["CCO"]})
        assert result.isError is False
        assert result.structuredContent is not None
        verdict = result.structuredContent["verdict"]
        assert "not a negative mutagenicity prediction" in verdict
        assert "ICH M7" in verdict and "purge factor" in verdict


async def test_a_miss_comes_back_as_a_result_not_an_error(running_server: str) -> None:
    """An ICH miss is a result, not an error, so the agent does not recite a limit instead.

    The miss carries `limit: null` and says the tables do not carry the substance, not that no limit
    exists.
    """
    async with _session(running_server) as session:
        result = await session.call_tool("ich_impurity_limit", {"substance": "unobtainium"})
        assert result.isError is False
        assert result.structuredContent is not None
        assert result.structuredContent["limit"] is None
        assert "not that no limit exists" in result.structuredContent["verdict"]


async def test_a_transcribed_limit_survives_the_wire_with_its_citation(running_server: str) -> None:
    """The nested result a chemist puts in a report, and the citation that lets them."""
    async with _session(running_server) as session:
        result = await session.call_tool("ich_impurity_limit", {"substance": "Pd"})
        assert result.isError is False
        assert result.structuredContent is not None
        limit = result.structuredContent["limit"]
        assert limit["substance"] == "Palladium (Pd)"
        assert ("oral PDE", 100.0, "µg/day") in {
            (entry["basis"], entry["value"], entry["unit"]) for entry in limit["limits"]
        }
        assert limit["citation"].startswith("ICH Q3D(R2)")


async def test_a_refused_structure_reaches_the_agent_as_a_usable_message(
    running_server: str,
) -> None:
    """A deliberately worded domain error passes through; an internal one would not.

    `SafetyRulesError` is a `ValueError` so `connector_app` passes it on. RDKit reads
    `"CCO CCCN=[N+]=[N-]"` as ethanol, so the alternative to a readable refusal is a clean screen of
    the wrong molecule.
    """
    async with _session(running_server) as session:
        result = await session.call_tool("screen_hazards", {"smiles": ["CCO CCCN=[N+]=[N-]"]})
        assert result.isError is True
        assert "invalid SMILES" in str(result.content)
