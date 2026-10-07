"""The server as Chemclaw3 meets it: a real socket, a real MCP handshake, a real 401.

Runs uvicorn on loopback and talks to it the way the agent will, so a session manager nobody
ran, an unchecked bearer credential, or a manifest that disagrees with the served surface fails
here.
"""

from __future__ import annotations

import math
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from chemclaw_mcp_unitops.engine import distillation, mixing, selftest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp_server_kit.testing import assert_bearer_is_enforced, assert_manifest_matches

TOKEN = "test-token-for-unitops"
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

    os.environ["CHEMCLAW_UNITOPS_TOKEN"] = TOKEN
    from chemclaw_mcp_unitops.app import app

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
        pytest.fail("the unitops server did not become ready within 30 s")
    yield base
    server.should_exit = True
    thread.join(timeout=10)


def test_healthz_answers_and_names_the_server(running_server: str) -> None:
    """Uvicorn accepts connections only after the lifespan ran, so a 200 here means it did."""
    body = httpx.get(f"{running_server}/healthz", timeout=5.0).json()
    assert body["status"] == "ok"
    assert body["server"] == "unitops"


def test_healthz_names_the_correlations_this_pod_verified(running_server: str) -> None:
    """The half a constant 200 does not have: what was checked, and which version of it."""
    body = httpx.get(f"{running_server}/healthz", timeout=5.0).json()
    assert body["datasets"] == [f"unitops-correlations@{selftest.CONSTANTS_VERSION}"]


def test_the_readiness_probe_refuses_when_zwieterings_exponents_are_transposed() -> None:
    """Break a dependency: transposed Zwietering exponents make the readiness probe unready.

    Swapping the particle-diameter and buoyancy exponents (a realistic transcription error) gives a
    plausible `N_js`, but the set no longer makes a frequency. A probe that only checked the
    correlation was callable would pass this pod.
    """
    if selftest.verify() is None:  # pragma: no cover - verify returns a list or raises
        pytest.fail("the probe must pass on an unmodified build")

    original = dict(mixing.ZWIETERING_EXPONENTS)
    mixing.ZWIETERING_EXPONENTS["particle_diameter"] = original["buoyancy"]
    mixing.ZWIETERING_EXPONENTS["buoyancy"] = original["particle_diameter"]
    try:
        with pytest.raises(selftest.SelfTestFailed, match="frequency"):
            selftest.verify()
    finally:
        mixing.ZWIETERING_EXPONENTS.update(original)

    assert selftest.verify(), "the probe must recover once the exponents are restored"


def test_the_readiness_probe_refuses_when_a_correlation_stops_matching_its_closed_form() -> None:
    """The probe refuses when a correlation stops matching a closed form written only in the probe.

    Narrowing Underwood's bisection to one halving leaves a plausible root and reflux that no longer
    reproduce the binary closed form.
    """
    original = distillation.UNDERWOOD_BISECTION_STEPS
    distillation.UNDERWOOD_BISECTION_STEPS = 1
    try:
        with pytest.raises(selftest.SelfTestFailed, match="minimum reflux"):
            selftest.verify()
    finally:
        distillation.UNDERWOOD_BISECTION_STEPS = original

    assert selftest.verify(), "the probe must recover once the solver is restored"


def test_a_failing_probe_is_a_permanent_cause_and_therefore_answers_503() -> None:
    """Only a permanent cause may take a pod out of its Service.

    A moved correlation does not improve under less load, so `SelfTestFailed` must classify into
    `PERMANENT_CAUSES` for the 503 to happen; asserted against the kit's own classifier.
    """
    from mcp_server_kit.degradation import PERMANENT_CAUSES, classify

    assert classify(selftest.SelfTestFailed("a moved correlation")) in PERMANENT_CAUSES


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

    The value is the hand-computed one from `test_distillation.py` (6.4269 stages, R_min 1.100),
    carried through pydantic and JSON, so a unit error introduced by the surface shows here.
    """
    async with _session(running_server) as session:
        listed = await session.list_tools()
        assert "shortcut_distillation" in sorted(tool.name for tool in listed.tools)

        result = await session.call_tool(
            "shortcut_distillation",
            {
                "relative_volatility": 2.5,
                "light_key_in_feed": 0.5,
                "light_key_in_distillate": 0.95,
                "light_key_in_bottoms": 0.05,
            },
        )
        assert result.isError is False
        assert result.structuredContent is not None
        assert result.structuredContent["minimum_stages"] == pytest.approx(6.4269, abs=5.0e-5)
        assert result.structuredContent["minimum_reflux_ratio"] == pytest.approx(1.100, abs=1e-5)
        assert result.structuredContent["basis"]

        assert_manifest_matches(MANIFEST, listed.tools)


async def test_both_scale_up_criteria_reach_the_model_over_the_wire(running_server: str) -> None:
    """Both scale-up criteria reach the model over the wire.

    A nested result model is where a surface most easily loses half an answer, and returning one
    duty would be this server choosing a scale-up criterion.
    """
    async with _session(running_server) as session:
        result = await session.call_tool(
            "agitation_scale_up",
            {
                "small_impeller_diameter_m": 0.05,
                "small_speed_rpm": 500.0,
                "small_liquid_volume_m3": 0.001,
                "large_impeller_diameter_m": 0.45,
                "large_liquid_volume_m3": 0.25,
                "power_number": 1.5,
                "liquid_density_kg_per_m3": 880.0,
                "liquid_viscosity_pa_s": 0.0012,
            },
        )
        assert result.isError is False
        body = result.structuredContent
        assert body is not None
        assert body["matched_power_per_volume"]["power_per_volume_w_per_m3"] == pytest.approx(
            body["small"]["power_per_volume_w_per_m3"], rel=1.0e-9
        )
        assert body["matched_tip_speed"]["tip_speed_m_per_s"] == pytest.approx(
            body["small"]["tip_speed_m_per_s"], rel=1.0e-9
        )
        assert body["criteria_disagree_by"] > 1.0
        assert body["small"]["turbulent"] is True


async def test_the_optional_inversions_serialise_as_null_when_they_are_not_asked_for(
    running_server: str,
) -> None:
    """A `float | None` over the wire, which is the shape an optional answer has to keep.

    Asking for the time constant alone must not invent a time-to-target, and a field that
    serialised as 0.0 instead of null would read as "instantly" to a model.
    """
    async with _session(running_server) as session:
        result = await session.call_tool(
            "heat_transfer_time_constant",
            {
                "batch_mass_kg": 220.0,
                "heat_capacity_j_per_kg_k": 1900.0,
                "overall_heat_transfer_coefficient_w_per_m2_k": 300.0,
                "heat_transfer_area_m2": 2.1,
                "initial_temperature_c": 60.0,
                "jacket_temperature_c": -10.0,
            },
        )
        assert result.isError is False
        body = result.structuredContent
        assert body is not None
        assert body["time_to_target_seconds"] is None
        assert body["temperature_after_c"] is None
        assert body["time_constant_seconds"] > 0.0
        assert "not a process heat load" in body["basis"]


async def test_a_bad_input_reaches_the_agent_as_a_usable_message(running_server: str) -> None:
    """A deliberately worded domain error passes through; an internal one would not.

    `connector_app` decides by exception type, invisible from a direct call; a misclassified
    `UnitOpsInputError` would reach a chemist as an opaque `error_id`.
    """
    async with _session(running_server) as session:
        azeotrope = await session.call_tool(
            "shortcut_distillation",
            {
                "relative_volatility": 1.0,
                "light_key_in_feed": 0.5,
                "light_key_in_distillate": 0.95,
                "light_key_in_bottoms": 0.05,
            },
        )
        assert azeotrope.isError is True
        # Pydantic's own `gt=1` bound refuses this one before the engine sees it, which is the
        # right place for it — what matters is that the model is told which argument and why.
        assert "relative_volatility" in str(azeotrope.content)

        flat_curve = await session.call_tool(
            "crystallisation_yield",
            {
                "solute_charged_kg": 3.0,
                "solvent_charged_kg": 10.0,
                "solubility_hot_kg_per_kg_solvent": 0.30,
                "solubility_cold_kg_per_kg_solvent": 0.30,
            },
        )
        assert flat_curve.isError is True
        assert "anti-solvent" in str(flat_curve.content)


def test_the_readiness_probe_refuses_when_fenskes_logarithm_base_is_wrong() -> None:
    """The probe refuses when Fenske's logarithm base is wrong.

    The total-reflux check holds Molokanov's form for whatever N_min it is given, so it says nothing
    about Fenske itself. `log10` in place of `ln` is the realistic mistake (texts write `log` for
    both) and gives a plausible stage count off by a factor of 2.303.
    """
    if selftest.verify() is None:  # pragma: no cover - verify returns a list or raises
        pytest.fail("the probe must pass on an unmodified build")

    original = distillation.fenske_minimum_stages

    def base_ten(
        *,
        relative_volatility: float,
        light_key_in_distillate: float,
        light_key_in_bottoms: float,
    ) -> float:
        """`ln alpha` in the denominator read as `log10 alpha`."""
        top, bottom = light_key_in_distillate, light_key_in_bottoms
        ratio = (top / (1.0 - top)) * ((1.0 - bottom) / bottom)
        return math.log(ratio) / math.log10(relative_volatility)

    distillation.fenske_minimum_stages = base_ten
    try:
        with pytest.raises(selftest.SelfTestFailed, match="minimum stages"):
            selftest.verify()
    finally:
        distillation.fenske_minimum_stages = original

    assert selftest.verify(), "the probe must recover once the logarithm is restored"
