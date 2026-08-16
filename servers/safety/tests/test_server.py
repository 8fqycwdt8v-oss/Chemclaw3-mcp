"""The server as Chemclaw3 meets it: a real socket, a real MCP handshake, a real 401.

Everything else in this directory tests functions. This tests the *deployment surface* — and it is
the test that would have caught each of the three defects Chemclaw3 recorded on this exact seam:

- a mounted MCP app whose session manager nobody ran (accepts the connection, hangs on the call);
- a bearer credential the serving side never checked;
- a manifest that claimed a tool surface the server did not have.

So it runs uvicorn on a loopback port and talks to it the way the agent will. Two things here are
specific to `safety` and worth the extra tests. First, **the verdict has to survive the wire**:
every result this server produces carries a sentence saying what it is not, and that sentence is a
pydantic `computed_field` — a plain property would be dropped by serialization, and the caveat would
exist in the code, pass every unit test, and never reach the model writing the answer. Second, **a
refusal has to arrive as a readable message**: a SMILES this server cannot read in full is refused
rather than screened, and a chemist told "internal error" would re-ask the same malformed question.
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

    Module-scoped because a server start is the expensive part of this file, and every test here
    wants the same one. The bearer token is set in the environment the same way a deployment sets
    it, so the auth path under test is the deployed one rather than a stub.
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
    # `revision` is part of the probe payload since the handshake started carrying the
    # build (see `mcp_server_kit.app.server_revision`). "unknown" is the correct answer
    # for a test process, which is not built from a Containerfile — that the *image*
    # supplies a real one is asserted in `tests/test_fleet.py`, because a value nothing
    # fills is a provenance record that quietly says nothing.
    assert response.json() == {"status": "ok", "server": "safety", "revision": "unknown"}


def test_metrics_are_exposed_unauthenticated(running_server: str) -> None:
    """A Prometheus scrape has no identity and the exposition carries counts only."""
    response = httpx.get(f"{running_server}/metrics", timeout=5.0)
    assert response.status_code == 200
    assert "python_info" in response.text


def test_the_mcp_surface_refuses_an_unauthenticated_caller(running_server: str) -> None:
    """Chemclaw3's own `safety` bundle declares `auth: {mode: none}`; across a network it cannot."""
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
    client takes headers — which also means the credential is exercised on the real path rather than
    injected past it.
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
        assert_manifest_matches(MANIFEST, names)


async def test_a_clean_screen_arrives_carrying_its_disclaimer(running_server: str) -> None:
    """The one thing that must never be lost in transit: "no match" is not "safe".

    A `computed_field` rather than a plain property, because a plain property is not serialized —
    the caveat would be in the code, green under every unit test, and absent from the payload the
    model actually reads. Asserting it on the wire is the only place that distinction is visible.
    """
    async with _session(running_server) as session:
        result = await session.call_tool("screen_hazards", {"smiles": ["CCO"]})
        assert result.isError is False
        assert result.structuredContent is not None
        verdict = result.structuredContent["verdict"]
        assert result.structuredContent["flags"] == []
        assert "not a safety assessment" in verdict


async def test_a_clean_genotoxicity_screen_says_what_it_is_not(running_server: str) -> None:
    """The same property on the result that hedges hardest, and for a different reason.

    An empty alert list reads as "not mutagenic" — a (Q)SAR conclusion drawn from a nine-row table.
    The four things this system cannot produce are named individually in the payload because "expert
    assessment required" on its own did not stop a live run inventing an ICH M7 class and a worked
    purge factor.
    """
    async with _session(running_server) as session:
        result = await session.call_tool("screen_genotoxic_alerts", {"smiles": ["CCO"]})
        assert result.isError is False
        assert result.structuredContent is not None
        verdict = result.structuredContent["verdict"]
        assert "not a negative mutagenicity prediction" in verdict
        assert "ICH M7" in verdict and "purge factor" in verdict


async def test_a_miss_comes_back_as_a_result_not_an_error(running_server: str) -> None:
    """A miss is an answer. If it arrived as an error the agent would recite a limit instead.

    This is the tool that exists because a live run recited a palladium PDE from training, so the
    shape of a miss is the whole capability: `limit: null`, plus the sentence saying the tables do
    not carry the substance rather than that no limit exists.
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

    `SafetyRulesError` is a `ValueError` for exactly this reason — `connector_app` replaces every
    other exception with a generic notice — and it matters more here than on a calculator. RDKit
    reads `"CCO CCCN=[N+]=[N-]"` as ethanol and drops the azide after the space, so the alternative
    to a readable refusal is not an unhelpful error: it is a *clean screen of the wrong molecule*.
    """
    async with _session(running_server) as session:
        result = await session.call_tool("screen_hazards", {"smiles": ["CCO CCCN=[N+]=[N-]"]})
        assert result.isError is True
        assert "invalid SMILES" in str(result.content)
