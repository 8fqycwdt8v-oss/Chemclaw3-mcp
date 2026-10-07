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
from chemclaw_mcp_thermalsafety.engine import selftest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp_server_kit.testing import assert_bearer_is_enforced, assert_manifest_matches

TOKEN = "test-token-for-thermalsafety"
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

    os.environ["CHEMCLAW_THERMALSAFETY_TOKEN"] = TOKEN
    from chemclaw_mcp_thermalsafety.app import app

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
        pytest.fail("the thermalsafety server did not become ready within 30 s")
    yield base
    server.should_exit = True
    thread.join(timeout=10)


def test_healthz_answers_and_names_the_server(running_server: str) -> None:
    """Uvicorn accepts connections only after the lifespan ran, so a 200 here means it did."""
    response = httpx.get(f"{running_server}/healthz", timeout=5.0)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["server"] == "thermalsafety"
    # "unknown" is the correct answer for a test process, which is not built from a Containerfile;
    # that the *image* supplies a real one is asserted in `tests/test_fleet_*.py`.
    assert body["revision"] == "unknown"


def test_healthz_names_the_constants_this_pod_verified(running_server: str) -> None:
    """The half a constant 200 does not have: what was actually checked, and which version of it.

    An operator reading this can tell two pods apart without a shell on either — which is the whole
    reason `readiness` returns datasets rather than a boolean.
    """
    body = httpx.get(f"{running_server}/healthz", timeout=5.0).json()
    assert body["datasets"] == [f"thermalsafety-constants@{selftest.CONSTANTS_VERSION}"]


def test_the_readiness_probe_refuses_when_the_atomic_weight_table_is_wrong() -> None:
    """Break a dependency and read the status: a wrong atomic weight makes the probe unready.

    `ATOMIC_WEIGHTS` and the screening bands are a corpus living in source. Carbon is moved by
    1 g/mol and the probe, which recomputes TNT and nitroglycerine against published values, must
    refuse. Driven against the engine because it mutates a module-level table.
    """
    from chemclaw_mcp_thermalsafety.engine import oxygen_balance

    assert selftest.verify(), "the probe must pass on an unmodified build"

    original = oxygen_balance.ATOMIC_WEIGHTS["C"]
    oxygen_balance.ATOMIC_WEIGHTS["C"] = original + 1.0
    try:
        with pytest.raises(selftest.SelfTestFailed, match="atomic-weight table"):
            selftest.verify()
    finally:
        oxygen_balance.ATOMIC_WEIGHTS["C"] = original

    assert selftest.verify(), "the probe must recover once the table is restored"


def test_a_failing_probe_is_a_permanent_cause_and_therefore_answers_503() -> None:
    """Only a permanent cause may take a pod out of its Service.

    A wrong constants table does not improve under less load, so `SelfTestFailed` must classify into
    `PERMANENT_CAUSES` for `connector_app` to answer 503; asserted against the kit's classifier.
    """
    from mcp_server_kit.degradation import PERMANENT_CAUSES, classify

    assert classify(selftest.SelfTestFailed("a wrong table")) in PERMANENT_CAUSES


def test_metrics_are_exposed_unauthenticated(running_server: str) -> None:
    """A Prometheus scrape has no identity, and the exposition carries nothing about a request.

    The no-caller/session/argument rule is asserted over the live exposition for every server in
    `packages/mcp_server_kit/tests/test_connector_app.py`.
    """
    response = httpx.get(f"{running_server}/metrics", timeout=5.0)
    assert response.status_code == 200


def test_livez_answers_without_consulting_anything(running_server: str) -> None:
    """Liveness and readiness are different questions, and on this server they are the same answer.

    Asserted anyway, because the Deployment points the two probes at two routes and a 404 on
    `/livez` would be a kill loop rather than a missing feature.
    """
    assert httpx.get(f"{running_server}/livez", timeout=5.0).status_code == 200


async def test_the_bearer_credential_is_enforced_on_the_mounted_mcp_surface(
    running_server: str,
) -> None:
    """The bearer credential is enforced on the mounted MCP surface, driven against the running server.

    A mount bypasses the enclosing app's dependencies, so this cannot be read off the source.
    `assert_bearer_is_enforced` drives every arm once for the whole fleet.
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


async def test_a_real_mcp_session_lists_and_calls_a_tool(running_server: str) -> None:
    """The handshake plus a tool call, and the manifest checked against the running surface.

    The value is the hand-computed 15.789 K from `test_runaway.py`, carried through pydantic and
    JSON, so a unit error introduced by the surface shows here.
    """
    async with _session(running_server) as session:
        listed = await session.list_tools()
        names = sorted(tool.name for tool in listed.tools)
        assert "adiabatic_temperature_rise" in names

        result = await session.call_tool(
            "adiabatic_temperature_rise",
            {
                "heat_of_reaction_kj_per_mol": -150.0,
                "moles": 10.0,
                "mass_kg": 50.0,
                "specific_heat_kj_per_kg_k": 1.9,
            },
        )
        assert result.isError is False
        assert result.structuredContent is not None
        assert result.structuredContent["adiabatic_temperature_rise_k"] == pytest.approx(
            1500.0 / 95.0
        )
        assert result.structuredContent["basis"]

        # The manifest is a claim about this surface; here is where it is checked against the
        # server that is actually running — including that all seven are classified exactly once.
        assert_manifest_matches(MANIFEST, listed.tools)


async def test_a_bad_input_reaches_the_agent_as_a_usable_message(running_server: str) -> None:
    """A deliberately worded domain error passes through; an internal one would not.

    `connector_app` decides by exception type, invisible from a direct call; a misclassified
    `ThermalInputError` or `FormulaError` would reach a chemist as an opaque `error_id`.
    """
    async with _session(running_server) as session:
        refused = await session.call_tool(
            "mtsr",
            {
                "process_temperature_c": 20.0,
                "adiabatic_temperature_rise_k": 100.0,
                "accumulation_fraction": 30.0,
            },
        )
        assert refused.isError is True
        assert "accumulation_fraction" in str(refused.content)

        bad_formula = await session.call_tool(
            "oxygen_balance_screen", {"molecular_formula": "Ca(NO3)2"}
        )
        assert bad_formula.isError is True
        assert "parentheses" in str(bad_formula.content)


async def test_the_semenov_answer_carries_its_not_an_sadt_disclaimer_over_the_wire(
    running_server: str,
) -> None:
    """The Semenov "not an SADT" disclaimer survives serialisation, where the model reads it.

    A `basis` dropped by the response model is invisible to direct-call tests.
    """
    async with _session(running_server) as session:
        result = await session.call_tool(
            "semenov_critical_ambient",
            {
                "mass_kg": 25.0,
                "heat_release_rate_w_per_kg": 1.0,
                "reference_temperature_c": 100.0,
                "activation_energy_kj_per_mol": 140.0,
                "heat_transfer_coefficient_w_per_m2_k": 5.0,
                "surface_area_m2": 0.5,
            },
        )
        assert result.isError is False
        assert result.structuredContent is not None
        assert "NOT an SADT" in result.structuredContent["basis"]
        assert result.structuredContent["rounded_up_to_five_c"] % 5 == 0


async def test_an_oversized_formula_is_refused_at_the_transport(running_server: str) -> None:
    """The bound is on the surface, not just in the docstring — and it is what the caller meets.

    `@server.tool()` returns the undecorated function, so a direct call skips argument validation
    entirely: a bound that is only real over the wire has to be asserted over the wire.
    """
    async with _session(running_server) as session:
        result = await session.call_tool("oxygen_balance_screen", {"molecular_formula": "C" * 5000})
        assert result.isError is True
        assert "at most" in str(result.content)
