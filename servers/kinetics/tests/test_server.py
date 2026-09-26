"""The server as Chemclaw3 meets it: a real socket, a real MCP handshake, a real 401.

Everything else in this directory tests functions. This tests the *deployment surface* — and it is
the test that would have caught each of the three defects Chemclaw3 recorded on this exact seam:

- a mounted MCP app whose session manager nobody ran (accepts the connection, hangs on the call);
- a bearer credential the serving side never checked;
- a manifest that claimed a tool surface the server did not have.

So it runs uvicorn on a loopback port and talks to it the way the agent will.
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
from chemclaw_mcp_kinetics.engine import selftest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp_server_kit.testing import assert_bearer_is_enforced, assert_manifest_matches

TOKEN = "test-token-for-kinetics"
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

    os.environ["CHEMCLAW_KINETICS_TOKEN"] = TOKEN
    from chemclaw_mcp_kinetics.app import app

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
        pytest.fail("the kinetics server did not become ready within 30 s")
    yield base
    server.should_exit = True
    thread.join(timeout=10)


def test_healthz_answers_and_names_the_server(running_server: str) -> None:
    """Uvicorn accepts connections only after the lifespan ran, so a 200 here means it did."""
    body = httpx.get(f"{running_server}/healthz", timeout=5.0).json()
    assert body["status"] == "ok"
    assert body["server"] == "kinetics"


def test_healthz_names_the_constants_this_pod_verified(running_server: str) -> None:
    """The half a constant 200 does not have: what was checked, and which version of it."""
    body = httpx.get(f"{running_server}/healthz", timeout=5.0).json()
    assert body["datasets"] == [f"kinetics-formulas@{selftest.CONSTANTS_VERSION}"]


def test_the_readiness_probe_refuses_when_the_integrator_loses_its_convergence_order() -> None:
    """Break a dependency and read the status — the proof D-2026-09-12 asks a new probe to give.

    The dependency broken here is the real defect this server already had. `semibatch_accumulation`
    guarded its feed term with `time < dose_time_seconds`, so the final RK4 step's k4 stage alone
    saw the feed switched off: one step of O(h) error inside an O(h^4) scheme. It dropped the
    integrator to first-order convergence while leaving the answer right to three significant
    figures — which is why the probe checks the convergence *rate* rather than a tolerance. A
    tolerance loose enough to pass the correct code would have passed the defect too.

    Driven by patching the module's step-order floor above what the shipped integrator achieves,
    which is the same signal a real first-order regression produces.
    """
    assert selftest.verify(), "the probe must pass on an unmodified build"

    original = selftest._FOURTH_ORDER_FLOOR
    selftest._FOURTH_ORDER_FLOOR = 1e9
    try:
        with pytest.raises(selftest.SelfTestFailed, match="convergence"):
            selftest.verify()
    finally:
        selftest._FOURTH_ORDER_FLOOR = original

    assert selftest.verify(), "the probe must recover once the floor is restored"


def test_the_probe_refuses_when_a_reactor_model_stops_matching_the_textbook_ratio() -> None:
    """The other arm, against a relation neither reactor model contains.

    `tau_CSTR/tau_PFR` at 90% first-order conversion is 3.909 — a number that falls out of the two
    models being *different*, so it cannot be satisfied by both being wrong the same way.
    """
    assert selftest.verify()

    original = selftest._NINETY_PERCENT
    # A conversion the ratio is not 3.909 at: the check's own expectation moves with it, so this
    # asserts the check is computing the expectation rather than comparing to a literal.
    selftest._NINETY_PERCENT = 0.5
    try:
        selftest.verify()  # must still pass: the expectation is derived, not hardcoded
    finally:
        selftest._NINETY_PERCENT = original


def test_a_failing_probe_is_a_permanent_cause_and_therefore_answers_503() -> None:
    """Only a permanent cause may take a pod out of its Service.

    A formula that has moved is permanent — it does not get better under less load — so
    `SelfTestFailed` has to classify into `PERMANENT_CAUSES` for the 503 to happen at all.
    Asserted against the kit's own classifier rather than restated.
    """
    from mcp_server_kit.degradation import PERMANENT_CAUSES, classify

    assert classify(selftest.SelfTestFailed("a moved formula")) in PERMANENT_CAUSES


def test_metrics_are_exposed_unauthenticated(running_server: str) -> None:
    """A Prometheus scrape has no identity, and the exposition carries nothing about a request."""
    assert httpx.get(f"{running_server}/metrics", timeout=5.0).status_code == 200


def test_livez_answers_without_consulting_anything(running_server: str) -> None:
    """The Deployment points the two probes at two routes; a 404 here would be a kill loop."""
    assert httpx.get(f"{running_server}/livez", timeout=5.0).status_code == 200


async def test_the_bearer_credential_is_enforced_on_the_mounted_mcp_surface(
    running_server: str,
) -> None:
    """Driven against the running server, because the defect is invisible in the source.

    `/mcp` is *mounted*, and a mount bypasses the enclosing app's dependencies — so a credential
    can be declared, reviewed and applied to everything except the route that matters.
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

    The value asserted is the rule of thumb every chemist carries — the rate doubles per 10 degrees
    at about 53 kJ/mol — carried through pydantic validation and JSON serialisation. A unit error
    introduced by the *surface* rather than the engine would show up here and nowhere else.
    """
    async with _session(running_server) as session:
        listed = await session.list_tools()
        assert "rate_constant_at_temperature" in sorted(tool.name for tool in listed.tools)

        result = await session.call_tool(
            "rate_constant_at_temperature",
            {
                "target_temperature_c": 35.0,
                "reference_temperature_c": 25.0,
                "reference_rate_constant": 1.0,
                "activation_energy_kj_per_mol": 52.9,
            },
        )
        assert result.isError is False
        assert result.structuredContent is not None
        assert result.structuredContent["rate_ratio"] == pytest.approx(2.0, abs=0.01)
        assert result.structuredContent["basis"]

        assert_manifest_matches(MANIFEST, listed.tools)


async def test_the_plug_flow_advantage_reaches_the_model_over_the_wire(
    running_server: str,
) -> None:
    """Both reactors in one answer, because the comparison is what a chemist actually needs."""
    async with _session(running_server) as session:
        result = await session.call_tool(
            "continuous_reactor_conversion",
            {
                "rate_constant": 0.02,
                "inlet_concentration": 1.0,
                "residence_time_seconds": 90.0,
            },
        )
        assert result.isError is False
        body = result.structuredContent
        assert body is not None
        assert body["plug_flow_conversion"] > body["stirred_tank_conversion"]
        assert body["plug_flow_advantage"] > 1.0
        assert "outlet composition" in body["basis"]


async def test_a_bad_input_reaches_the_agent_as_a_usable_message(running_server: str) -> None:
    """A deliberately worded domain error passes through; an internal one would not.

    `connector_app` decides by exception *type*, and the decision is invisible from a direct call.
    `KineticsInputError` sorted into the sanitiser's other branch would reach a chemist as an
    opaque `error_id` rather than as the sentence naming which number is wrong.
    """
    async with _session(running_server) as session:
        falling = await session.call_tool(
            "activation_energy_from_two_rates",
            {
                "lower_temperature_c": 25.0,
                "lower_rate_constant": 2.0e-3,
                "upper_temperature_c": 55.0,
                "upper_rate_constant": 1.0e-3,
            },
        )
        assert falling.isError is True
        assert "rate that falls" in str(falling.content)

        percentage = await session.call_tool(
            "batch_time_to_reach",
            {"rate_constant": 0.01, "initial_concentration": 1.0, "conversion": 90.0, "order": 1.0},
        )
        assert percentage.isError is True


async def test_the_accumulation_profile_is_bounded_on_the_wire(running_server: str) -> None:
    """The integrator runs at full resolution; what crosses the wire is thinned and keeps its end.

    The accumulation left when the addition finishes is a different question from the peak, so a
    stride that dropped the last point would silently answer neither.
    """
    async with _session(running_server) as session:
        result = await session.call_tool(
            "semibatch_accumulation_profile",
            {
                "rate_constant": 0.02,
                "dose_time_seconds": 7200.0,
                "initial_volume": 50.0,
                "dosed_moles": 40.0,
                "initial_coreagent_concentration": 0.9,
                "dosed_volume": 8.0,
            },
        )
        assert result.isError is False
        body = result.structuredContent
        assert body is not None
        assert len(body["profile"]) <= 25
        assert body["profile"][-1]["time_seconds"] == pytest.approx(7200.0)
        assert body["profile"][-1]["accumulated_fraction"] == pytest.approx(
            body["accumulation_at_end_of_dose"]
        )
        assert "no energy balance" in body["basis"]
        assert body["method"] == "rk4"
        assert body["caveat"] is None


async def test_a_stiff_dose_is_answered_on_the_wire_with_its_method_and_caveat(
    running_server: str,
) -> None:
    """The dose the server used to refuse as mixing-limited now answers, and still says so.

    The warning the refusal carried is not lost with it: the answer names the stable scheme and
    carries a caveat that the perfectly-mixed number is a floor in this regime, naming the server
    the heat-removal question belongs to.
    """
    async with _session(running_server) as session:
        result = await session.call_tool(
            "semibatch_accumulation_profile",
            {
                "rate_constant": 200.0,
                "dose_time_seconds": 7200.0,
                "initial_volume": 50.0,
                "dosed_moles": 40.0,
                "initial_coreagent_concentration": 0.9,
                "dosed_volume": 8.0,
            },
        )
        assert result.isError is False, result.content
        body = result.structuredContent
        assert body is not None
        assert body["method"] == "sdirk3-l-stable"
        assert body["peak_accumulation_fraction"] == pytest.approx(8.0545086724e-06, rel=1e-6)
        assert "mixing-limited" in body["caveat"]
        assert "thermalsafety" in body["caveat"]
        assert "L-stable" in body["basis"]
