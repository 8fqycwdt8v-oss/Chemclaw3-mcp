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
from chemclaw_mcp_suitability.engine import selftest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp_server_kit.testing import assert_bearer_is_enforced, assert_manifest_matches

TOKEN = "test-token-for-suitability"
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

    os.environ["CHEMCLAW_SUITABILITY_TOKEN"] = TOKEN
    from chemclaw_mcp_suitability.app import app

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
        pytest.fail("the suitability server did not become ready within 30 s")
    yield base
    server.should_exit = True
    thread.join(timeout=10)


def test_healthz_answers_and_names_the_server(running_server: str) -> None:
    """Uvicorn accepts connections only after the lifespan ran, so a 200 here means it did."""
    body = httpx.get(f"{running_server}/healthz", timeout=5.0).json()
    assert body["status"] == "ok"
    assert body["server"] == "suitability"


def test_healthz_names_the_constants_this_pod_verified(running_server: str) -> None:
    """The half a constant 200 does not have: what was checked, and which version of it."""
    body = httpx.get(f"{running_server}/healthz", timeout=5.0).json()
    assert body["datasets"] == [f"suitability-constants@{selftest.CONSTANTS_VERSION}"]


def test_the_readiness_probe_refuses_when_a_usp_constant_is_transposed() -> None:
    """Break a dependency and read the status: a transposed USP constant makes the probe unready.

    The second form of each plate and resolution pair is derived from the first, so on a Gaussian
    peak they must agree; a probe that only checked each function returns a float would pass a
    typo. Driven against the engine because it patches a module constant; `/healthz`'s use of the
    result is asserted above.
    """
    assert selftest.verify(), "the probe must pass on an unmodified build"

    original = selftest._PLATE_AGREEMENT
    # Tightening the agreement tolerance below the published rounding produces the same signal an
    # inconsistent constant would; the next test drives the transposition itself.
    selftest._PLATE_AGREEMENT = 1e-9
    try:
        with pytest.raises(selftest.SelfTestFailed, match="plate-count conventions disagree"):
            selftest.verify()
    finally:
        selftest._PLATE_AGREEMENT = original

    assert selftest.verify(), "the probe must recover once the tolerance is restored"


def test_a_transposed_plate_constant_would_break_the_agreement_the_probe_checks() -> None:
    """A transposed plate constant would break the agreement the probe checks.

    5.54 gives 0.09% disagreement and 5.45 gives 1.7%, well past the probe's 0.3%, so the tolerance
    discriminates a real transposition from published rounding.
    """
    sigma, retention = 0.05, 6.0
    tangent = 16.0 * (retention / (4.0 * sigma)) ** 2
    half_width = selftest._HALF_HEIGHT_WIDTHS_PER_SIGMA * sigma

    correct = 5.54 * (retention / half_width) ** 2
    transposed = 5.45 * (retention / half_width) ** 2

    assert abs(tangent - correct) / tangent == pytest.approx(0.00093, abs=0.0002)
    assert abs(tangent - transposed) / tangent == pytest.approx(0.0171, abs=0.0005)
    assert abs(tangent - correct) / tangent < selftest._PLATE_AGREEMENT
    assert abs(tangent - transposed) / tangent > selftest._PLATE_AGREEMENT


def test_a_failing_probe_is_a_permanent_cause_and_therefore_answers_503() -> None:
    """Only a permanent cause may take a pod out of its Service.

    A wrong constant does not improve under less load, so `SelfTestFailed` must classify into
    `PERMANENT_CAUSES` for `connector_app` to answer 503; asserted against the kit's classifier.
    """
    from mcp_server_kit.degradation import PERMANENT_CAUSES, classify

    assert classify(selftest.SelfTestFailed("a wrong constant")) in PERMANENT_CAUSES


def test_metrics_are_exposed_unauthenticated(running_server: str) -> None:
    """A Prometheus scrape has no identity, and the exposition carries nothing about a request."""
    assert httpx.get(f"{running_server}/metrics", timeout=5.0).status_code == 200


def test_livez_answers_without_consulting_anything(running_server: str) -> None:
    """The Deployment points the two probes at two routes; a 404 here would be a kill loop."""
    assert httpx.get(f"{running_server}/livez", timeout=5.0).status_code == 200


async def test_the_bearer_credential_is_enforced_on_the_mounted_mcp_surface(
    running_server: str,
) -> None:
    """The bearer credential is enforced on the mounted MCP surface, against the running server.

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

    The value is the one `test_precision.py` computes from the eval corpus, carried through pydantic
    and JSON, so a denominator error introduced by the surface shows here.
    """
    async with _session(running_server) as session:
        listed = await session.list_tools()
        assert "replicate_precision" in sorted(tool.name for tool in listed.tools)

        result = await session.call_tool(
            "replicate_precision",
            {
                "values": [1024110.0, 1038902.0, 1001455.0, 1061203.0, 998774.0, 1049318.0],
                "limit_percent": 2.0,
            },
        )
        assert result.isError is False
        assert result.structuredContent is not None
        assert result.structuredContent["relative_standard_deviation_percent"] == pytest.approx(
            2.476, abs=0.001
        )
        assert result.structuredContent["meets_limit"] is False
        assert result.structuredContent["basis"]

        # The manifest is a claim about this surface; here is where it is checked against the
        # server actually running — including that all seven are classified exactly once.
        assert_manifest_matches(MANIFEST, listed.tools)


async def test_the_convention_reaches_the_answer_over_the_wire(running_server: str) -> None:
    """A plate count's convention survives the wire.

    A response model could drop the field unseen by a direct call, and a count without its
    convention is not comparable with a limit.
    """
    async with _session(running_server) as session:
        result = await session.call_tool(
            "plate_count",
            {"retention_time_min": 6.0, "width_min": 0.2, "convention": "half_height"},
        )
        assert result.isError is False
        assert result.structuredContent is not None
        assert result.structuredContent["convention"] == "half_height"
        assert "5.54" in result.structuredContent["basis"]


async def test_the_gaussian_assumption_is_flagged_in_the_serialised_resolution(
    running_server: str,
) -> None:
    """The half-height form is the optimistic one, and the flag is how a caller finds that out."""
    async with _session(running_server) as session:
        result = await session.call_tool(
            "peak_resolution",
            {
                "first_retention_min": 5.0,
                "second_retention_min": 5.4,
                "first_width_min": 0.2,
                "second_width_min": 0.21,
                "convention": "half_height",
            },
        )
        assert result.isError is False
        assert result.structuredContent is not None
        assert result.structuredContent["assumes_gaussian"] is True
        assert "approximates on a tailing peak" in result.structuredContent["basis"]


async def test_a_bad_input_reaches_the_agent_as_a_usable_message(running_server: str) -> None:
    """A deliberately worded domain error passes through; an internal one would not.

    `connector_app` decides by exception type, invisible from a direct call; a misclassified
    `PeakError`, `PrecisionError` or `AdjustmentError` would reach a chemist as an opaque
    `error_id`.
    """
    async with _session(running_server) as session:
        reversed_peaks = await session.call_tool(
            "peak_resolution",
            {
                "first_retention_min": 5.4,
                "second_retention_min": 5.0,
                "first_width_min": 0.2,
                "second_width_min": 0.2,
                "convention": "tangent",
            },
        )
        assert reversed_peaks.isError is True
        assert "not after" in str(reversed_peaks.content)

        gradient = await session.call_tool(
            "permitted_method_adjustment",
            {"parameter": "flow_rate", "original": 1.0, "proposed": 1.2, "is_gradient": True},
        )
        assert gradient.isError is True
        assert "isocratic" in str(gradient.content)

        flat = await session.call_tool("replicate_precision", {"values": [-1.0, 1.0]})
        assert flat.isError is True
        assert "undefined" in str(flat.content)


async def test_an_oversized_injection_series_is_refused_at_the_transport(
    running_server: str,
) -> None:
    """The bound is on the surface, not just in the docstring — and it is what the caller meets.

    `@server.tool()` returns the undecorated function, so a direct call skips the check the caller
    actually hits. A bound that is only real over the wire has to be asserted over the wire.
    """
    async with _session(running_server) as session:
        result = await session.call_tool("replicate_precision", {"values": [100.0] * 1500})
        assert result.isError is True
        assert "more than this tool will take" in str(result.content)


async def test_the_report_composes_the_primitives_and_lists_only_declared_failures(
    running_server: str,
) -> None:
    """The report composes the primitives and lists only declared failures.

    An undeclared criterion is neither checked nor listed, so `failures: []` must not be read as
    "suitable"; asserted rather than left to the docstring.
    """
    async with _session(running_server) as session:
        result = await session.call_tool(
            "system_suitability_report",
            {
                "peak_table": [
                    {
                        "name": "impurity A",
                        "retention_time_min": 3.10,
                        "width_min": 0.18,
                        "width_at_5_percent_min": 0.36,
                        "leading_half_width_min": 0.15,
                    },
                    {
                        "name": "main",
                        "retention_time_min": 4.09,
                        "width_min": 0.20,
                        "width_at_5_percent_min": 0.46,
                        "leading_half_width_min": 0.10,
                    },
                ],
                "convention": "tangent",
                "replicate_areas": [
                    1024110.0,
                    1038902.0,
                    1001455.0,
                    1061203.0,
                    998774.0,
                    1049318.0,
                ],
                "replicate_retention_times_min": [],
                "area_rsd_limit_percent": 2.0,
                "maximum_tailing_factor": 2.0,
                # `minimum_resolution` and `minimum_plates` are deliberately not declared.
            },
        )
        assert result.isError is False
        body = result.structuredContent
        assert body is not None

        # Both declared criteria fail: the area RSD is 2.476% against 2.0, and the main peak's
        # tailing factor is 0.46/(2 x 0.10) = 2.30 against 2.0.
        assert len(body["failures"]) == 2
        assert any("area RSD" in failure for failure in body["failures"])
        assert any("tailing factor" in failure for failure in body["failures"])

        # Resolution was computed and reported, but nothing was declared, so it is not a failure.
        assert body["peaks"][1]["resolution_from_previous"] is not None
        assert not any("resolution" in failure for failure in body["failures"])

        # And the number matches what the standalone tool gives for the same peaks.
        standalone = await session.call_tool(
            "peak_resolution",
            {
                "first_retention_min": 3.10,
                "second_retention_min": 4.09,
                "first_width_min": 0.18,
                "second_width_min": 0.20,
                "convention": "tangent",
            },
        )
        assert standalone.structuredContent is not None
        assert body["peaks"][1]["resolution_from_previous"] == pytest.approx(
            standalone.structuredContent["resolution"]
        )
