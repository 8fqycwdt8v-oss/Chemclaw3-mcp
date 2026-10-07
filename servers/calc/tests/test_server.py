"""The server as Chemclaw3 meets it: a real socket, a real MCP handshake, a real 401.

This tests the deployment surface: the session manager runs, the bearer credential is checked,
and the manifest matches the served tools. Specific to `calc`: `calc_version` and `calc_key` must
survive the wire (a projection could drop them unnoticed by unit tests), and a domain refusal must
arrive as a readable message, since `CalculationDomainError` is a `ValueError` precisely so the
sanitiser passes it through.
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
from chemclaw_mcp_calc import tools
from chemclaw_mcp_calc.engine import crest_cli
from chemclaw_mcp_calc.engine.admission import Admission
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp_server_kit.testing import assert_bearer_is_enforced, assert_manifest_matches

TOKEN = "test-token-for-calc"
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

    os.environ["CHEMCLAW_CALC_TOKEN"] = TOKEN
    from chemclaw_mcp_calc.app import app

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
        pytest.fail("the calc server did not become ready within 30 s")
    yield base
    server.should_exit = True
    thread.join(timeout=10)


def test_healthz_answers_and_names_the_server(running_server: str) -> None:
    """Uvicorn accepts connections only after the lifespan ran, so a 200 here means it did.

    Which also means the `on_start` hook was reached: `connector_app` starts it inside the same
    lifespan, so a server that could not schedule its version resolution would not be answering.
    """
    response = httpx.get(f"{running_server}/healthz", timeout=5.0)
    assert response.status_code == 200
    # `revision` is "unknown" in a test process, which is not built from a Containerfile; the fleet
    # tests assert the image supplies a real one.
    body = response.json()
    assert body["status"] == "ok"
    assert body["server"] == "calc"
    assert body["revision"] == "unknown"
    # Present and empty: this server vendors no corpus. Its readiness derives a `calc_version`, so a
    # pod that cannot resolve its backend answers 503.
    assert body["datasets"] == []


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
    path. The read timeout is set explicitly: past httpx's 5 s default the client treats the stream
    as dropped and reconnects, so a long CREST search would hang rather than fail.
    """
    async with (
        httpx.AsyncClient(
            headers={"Authorization": f"Bearer {TOKEN}"}, timeout=httpx.Timeout(300.0)
        ) as http_client,
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
        assert "compute_xtb_energy" in names

        result = await session.call_tool("compute_xtb_energy", {"smiles": "CCO"})
        assert result.isError is False
        assert result.structuredContent is not None
        assert result.structuredContent["total_energy_hartree"] < 0

        # Checked against the running server, with `Tool` objects rather than names, so a renamed or
        # retyped argument fails against `tool-surface.json`.
        assert_manifest_matches(MANIFEST, listed.tools)


async def test_the_version_and_the_key_survive_the_wire(running_server: str) -> None:
    """`calc_version` and `calc_key` survive the wire.

    A dropped field would pass every unit test and be missing at the consumer, which cannot rebuild
    it: the version depends on what is installed in this image.
    """
    async with _session(running_server) as session:
        for tool, arguments in (
            ("compute_xtb_energy", {"smiles": "CCO"}),
            ("predict_pka", {"smiles": "CC(=O)O"}),
            ("predict_solubility", {"smiles": "CCO"}),
            # The two that project through `model_copy`/`OptimizationSummary.of` on the way out.
            ("predict_site_reactivity", {"smiles": "CCO", "top_n": 3}),
            ("optimize_geometry", {"smiles": "O"}),
        ):
            result = await session.call_tool(tool, arguments)
            assert result.isError is False, f"{tool}: {result.content}"
            assert result.structuredContent is not None
            payload = result.structuredContent
            assert payload["calc_version"], f"{tool} arrived with no calc_version"
            assert payload["calc_key"].startswith(f"{payload['calc_key'].split('@')[0]}@"), tool
            assert payload["calc_version"] in payload["calc_key"], (
                f"{tool}: the key on the wire does not carry the version reported beside it"
            )


async def test_the_cache_probe_round_trips_and_matches_the_compute_it_precedes(
    running_server: str,
) -> None:
    """The cache probe round-trips and matches the compute it precedes, over the wire.

    `key` is a nested model and must arrive as an object reconstructing the same flat identity the
    compute tool stamps.
    """
    async with _session(running_server) as session:
        probe = await session.call_tool(
            "calculation_key", {"tool": "predict_solubility", "arguments": {"smiles": "CCO"}}
        )
        assert probe.isError is False, probe.content
        assert probe.structuredContent is not None
        key = probe.structuredContent["key"]
        assert set(key) == {"calc_type", "calc_version", "input_hash", "params_hash"}
        assert key["calc_type"] == "solubility"

        computed = await session.call_tool("predict_solubility", {"smiles": "CCO"})
        assert computed.isError is False
        assert computed.structuredContent is not None
        assert computed.structuredContent["calc_key"] == probe.structuredContent["calc_key"]
        assert computed.structuredContent["calc_version"] == probe.structuredContent["calc_version"]


async def test_the_probe_refuses_a_mistyped_argument_rather_than_keying_something_else(
    running_server: str,
) -> None:
    """The probe refuses a mistyped argument rather than keying something else.

    An ignored `solvant` would return the gas-phase key and answer a solvated question with an
    unsolvated row. `ValueError`, so the message reaches the caller.
    """
    async with _session(running_server) as session:
        result = await session.call_tool(
            "calculation_key",
            {"tool": "optimize_geometry", "arguments": {"smiles": "CCO", "solvant": "water"}},
        )
        assert result.isError is True
        assert "does not take 'solvant'" in str(result.content)


async def test_the_primitive_chain_composes_over_the_wire(running_server: str) -> None:
    """The primitive chain composes over the wire: embed, relax, differentiate.

    A `Structure` must survive being sent back in as an argument without changing its
    `structure_id`, and the base64 `.npy` Hessian must arrive intact.
    """
    import base64
    import io

    import numpy as np

    async with _session(running_server) as session:
        embedded = await session.call_tool("embed_structure", {"smiles": "O"})
        assert embedded.isError is False, embedded.content
        assert embedded.structuredContent is not None
        structure = embedded.structuredContent
        assert structure["structure_id"].startswith("st_")

        relaxed = await session.call_tool("relax_structure", {"structure": structure})
        assert relaxed.isError is False, relaxed.content
        assert relaxed.structuredContent is not None
        minimum = relaxed.structuredContent["structure"]
        # The optimised geometry carries the key of the calculation that produced it, so lineage
        # survives the hop rather than having to be reattached by the caller.
        assert minimum["origin"] == relaxed.structuredContent["calc_key"]

        hessian = await session.call_tool("compute_hessian", {"structure": minimum})
        assert hessian.isError is False, hessian.content
        assert hessian.structuredContent is not None
        payload = hessian.structuredContent
        matrix = np.load(io.BytesIO(base64.b64decode(payload["hessian_npy"])), allow_pickle=False)
        assert matrix.shape == (9, 9)
        assert np.allclose(matrix, matrix.T)
        assert payload["dipole_derivatives_npy"] is not None
        assert payload["structure_id"] == minimum["structure_id"]


async def test_a_crest_primitive_answers_or_refuses_by_name_across_the_wire(
    running_server: str,
) -> None:
    """A CREST primitive answers or refuses by name across the wire, whichever this deployment is.

    With a binary the ensemble must come back in the shape a composite consumes; without one the
    refusal must be a sentence naming the operator action. Branches on `is_available()` rather than
    skipping.
    """
    async with _session(running_server) as session:
        # Water, for the reason `test_calc_version.py` gives: one conformer and seconds of
        # sampling, so the *contract* is checked without a metadynamics run inside a unit suite.
        embedded = await session.call_tool("embed_structure", {"smiles": "O"})
        assert embedded.structuredContent is not None
        result = await session.call_tool(
            "search_conformer_ensemble", {"structure": embedded.structuredContent}
        )
        if crest_cli.is_available():
            assert result.isError is False
            assert result.structuredContent is not None
            assert result.structuredContent["members"]
            return
        assert result.isError is True
        assert "crest" in str(result.content)


async def test_a_domain_refusal_reaches_the_agent_as_a_usable_message(running_server: str) -> None:
    """A domain refusal reaches the agent as a usable message.

    `predict_pka`'s aliphatic-amine explanation tells the chemist to measure instead; as an opaque
    error the model would guess a reason.
    """
    async with _session(running_server) as session:
        result = await session.call_tool("predict_pka", {"smiles": "C1CCNCC1"})
        assert result.isError is True
        assert "Spearman -0.17" in str(result.content)


async def test_an_unparameterised_solvent_is_refused_by_name(running_server: str) -> None:
    """An unparameterised solvent (2-MeTHF) is refused by name, carrying the alternative.

    The check lives in `XtbSpec`'s validator; this verifies the message survives the transport.
    """
    async with _session(running_server) as session:
        result = await session.call_tool(
            "compute_electronic_properties",
            {"smiles": "CCO", "solvent": "2-methyltetrahydrofuran"},
        )
        assert result.isError is True
        assert "tetrahydrofuran" in str(result.content)


async def test_a_full_pod_refuses_with_a_marker_the_caller_can_classify(
    running_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A saturation refusal is distinguishable from a domain refusal on the wire.

    Chemclaw3 must retry "full" and not retry bad input. Driven over the real transport, which
    prefixes, re-wraps and flattens the message to one text block. Asserted on the literal rather
    than the constant, so it tests what crossed the wire.
    """
    monkeypatch.setattr(tools, "_admission", Admission(1))
    tools._admission.acquire("a calculation the test is pretending to run")
    async with _session(running_server) as session:
        result = await session.call_tool("predict_solubility", {"smiles": "CCO"})
    assert result.isError is True
    content = str(result.content)
    assert "[calc-at-capacity]" in content
    # And the human half is still there: a marker that replaced the message would leave the model
    # with nothing to say to the chemist.
    assert "0 of its 1 calculation slots free" in content


async def test_a_domain_refusal_does_not_carry_the_capacity_marker(running_server: str) -> None:
    """A domain refusal does not carry the capacity marker.

    Otherwise a bad molecule would be retried as backpressure.
    """
    async with _session(running_server) as session:
        result = await session.call_tool("predict_pka", {"smiles": "C1CCNCC1"})
    assert result.isError is True
    assert "[calc-at-capacity]" not in str(result.content)


async def test_a_time_budget_stop_carries_a_marker_the_caller_can_classify(
    running_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A time-budget stop carries its own marker on the wire.

    The same relaxation can finish idle and be stopped busy, so the caller must tell "needs more
    time" from "bad input". Asserted on the literal, at the head behind the transport's prefix,
    which is where Chemclaw3 matches.
    """
    from chemclaw_mcp_calc.engine.config import settings

    async with _session(running_server) as session:
        embedded = await session.call_tool("embed_structure", {"smiles": "CCO"})
        assert embedded.isError is False, embedded.content
        monkeypatch.setattr(settings, "xtb_inline_timeout_seconds", 1e-9)
        result = await session.call_tool(
            "relax_structure", {"structure": embedded.structuredContent}
        )
    assert result.isError is True
    content = str(result.content)
    assert "Error executing tool relax_structure: [calc-time-budget] " in content
    assert "[calc-at-capacity]" not in content
    assert "exceeded this server's inline budget" in content, "the human half is still there"


async def test_a_domain_refusal_does_not_carry_the_time_budget_marker(running_server: str) -> None:
    """The other direction: a marker on every refusal would excuse a bad molecule as a slow pod."""
    async with _session(running_server) as session:
        result = await session.call_tool("predict_pka", {"smiles": "C1CCNCC1"})
    assert result.isError is True
    assert "[calc-time-budget]" not in str(result.content)
