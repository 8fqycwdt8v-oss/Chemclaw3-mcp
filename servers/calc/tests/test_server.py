"""The server as Chemclaw3 meets it: a real socket, a real MCP handshake, a real 401.

Everything else in this directory tests functions. This tests the *deployment surface* — and it is
the test that would have caught each of the three defects Chemclaw3 recorded on this exact seam:

- a mounted MCP app whose session manager nobody ran (accepts the connection, hangs on the call);
- a bearer credential the serving side never checked;
- a manifest that claimed a tool surface the server did not have.

Two things here are specific to `calc`. First, **`calc_version` and `calc_key` have to survive the
wire**: they are ordinary pydantic fields rather than computed ones, but the tools return them
through `model_copy(update=...)` projections, and a field dropped there would pass every unit test
and arrive absent at the only consumer that matters. Second, **a domain refusal has to arrive as a
readable message**: `CalculationDomainError` is a `ValueError` precisely so `connector_app` passes
it through, and the aliphatic-amine explanation is the capability — a chemist told "internal error"
would try the next substrate instead of measuring it.
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
from chemclaw_mcp_calc.engine import crest_cli
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp_server_kit.testing import assert_manifest_matches

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

    Module-scoped because a server start is the expensive part of this file. The bearer token is set
    in the environment the same way a deployment sets it, so the auth path under test is the
    deployed one rather than a stub.
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
    # `revision` is part of the probe payload since the handshake started carrying the
    # build (see `mcp_server_kit.app.server_revision`). "unknown" is the correct answer
    # for a test process, which is not built from a Containerfile — that the *image*
    # supplies a real one is asserted in `tests/test_fleet.py`, because a value nothing
    # fills is a provenance record that quietly says nothing.
    body = response.json()
    assert body["status"] == "ok"
    assert body["server"] == "calc"
    assert body["revision"] == "unknown"
    # An empty list, and the field is present rather than omitted: this server vendors no corpus.
    # What the probe proves here is the other half of readiness — `app._readiness` derives a
    # `calc_version`, so a pod that could not resolve its backend answers 503 and is not sent a
    # calculation it would fail. `/healthz` was a constant 200 before that.
    assert body["datasets"] == []


def test_metrics_are_exposed_unauthenticated(running_server: str) -> None:
    """A Prometheus scrape has no identity, and the exposition carries nothing about a request.

    Not "counts only": the default registry publishes `python_info` and the `process_*`
    collectors. What an unauthenticated endpoint must never publish is a caller, a session, a
    correlation id or a tool argument — asserted over the live exposition in
    `packages/mcp_server_kit/tests/test_connector_app.py`, for every server at once.
    """
    response = httpx.get(f"{running_server}/metrics", timeout=5.0)
    assert response.status_code == 200


def test_the_mcp_surface_refuses_an_unauthenticated_caller(running_server: str) -> None:
    """Chemclaw3's own `calc` bundle declares `auth: {mode: none}`; across a network it cannot."""
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

    **The read timeout is stated rather than defaulted, and finding out why cost an afternoon.**
    httpx defaults to 5 s. A tool call that outlives it does not fail: the streamable-HTTP client
    treats the dropped stream as a disconnect and reconnects ("GET stream disconnected, reconnecting
    in 1000ms"), so the caller waits forever for a response that was already computed. Every tool
    here answered in milliseconds while `crest` shipped in no image, so the default was never
    reached; the first real CREST search over this wire hung the suite instead of failing it.
    Chemclaw3 sets the same bound deliberately on its side (`calc_sampling_timeout_seconds`).
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

        # The manifest is a claim about this surface; here is where the claim is checked against the
        # server that is actually running. The `Tool` objects go in rather than the names, because
        # the manifest declares *which* tools exist and says nothing about their arguments — so a
        # renamed or retyped argument is exactly the change that passes a name check and breaks
        # Chemclaw3 at call time. `tool-surface.json` beside the manifest is what records them.
        assert_manifest_matches(MANIFEST, listed.tools)


async def test_the_version_and_the_key_survive_the_wire(running_server: str) -> None:
    """The one field this whole port exists to deliver, asserted on the payload rather than in code.

    A `model_copy(update=...)` that dropped it, or a response model that stopped inheriting `Keyed`,
    would be green in every unit test and absent exactly here — at the consumer. And the consumer
    cannot rebuild it: `calc_version` is assembled from the tblite/RDKit builds and any `xtb` binary
    installed *in this image*, and `xtb_cli.binary_version()` answers `"absent"` rather than
    raising, so a client-side reconstruction is well-formed, matches zero calibration-ledger rows,
    and reads as `UNCALIBRATED` instead of as an error.
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
    """The integration this server exists for, exercised over the wire it will actually run on.

    Chemclaw3 calls `calculation_key`, looks the four fields up in its own store, and only reaches a
    compute tool on a miss. Both halves have to survive MCP serialization for that to work: `key` is
    a *nested model*, so it arrives as an object rather than a string, and it has to reconstruct the
    same flat identity the compute tool then stamps on its result. A unit test would not see a
    nesting the transport flattened or dropped.
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
    """The refusal that keeps a cache honest, checked where a caller will meet it.

    A silently ignored `solvant` would return the *gas-phase* key, the caller's lookup would hit a
    real row, and a solvated question would be answered by an unsolvated calculation with nothing
    anywhere saying so. `ValueError` is what makes the message reach the caller verbatim.
    """
    async with _session(running_server) as session:
        result = await session.call_tool(
            "calculation_key",
            {"tool": "optimize_geometry", "arguments": {"smiles": "CCO", "solvant": "water"}},
        )
        assert result.isError is True
        assert "does not take 'solvant'" in str(result.content)


async def test_the_primitive_chain_composes_over_the_wire(running_server: str) -> None:
    """The integration the durable jobs will actually run: embed, relax, differentiate.

    Every step of it crosses MCP as JSON, and two things about that only fail here. A `Structure`
    round-trips as a nested object and has to survive being sent *back in* as an argument — a
    coordinate lost to float formatting would change its `structure_id` and silently key a different
    geometry. And the Hessian is base64 `.npy`, megabytes of it at drug scale, so this is where the
    payload is proven to arrive intact rather than truncated.
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
    """Whichever of the two states this deployment is in, the caller can act on what comes back.

    Written when no image shipped `crest`, and it asserted the refusal — which is now the *other*
    branch, since `D-2026-08-26-a-sampler-nobody-ships-is-a-refusal-with-a-manual` puts the binary
    in the image. Both halves matter and neither may be assumed: with a binary the search has to
    come back as an ensemble across the wire (the shape a composite consumes), and without one the
    refusal has to be a **sentence**, because `connector_app` replaces every non-`ValueError` with a
    generic notice and the fix is an operator action the message must name.

    Branching on `is_available()` rather than skipping: a skip here would stop noticing the day a
    deployment trims the binary back out.
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
    """The measured failure this contract exists for, checked end to end.

    `predict_pka`'s aliphatic-amine explanation names the Spearman -0.17 correlation and tells the
    chemist to measure the value instead. In a live Chemclaw3 run it reached the model as an opaque
    "Error: Function failed"; the answer then guessed the reason and presented the guess as a fact
    about system behaviour. `CalculationDomainError` subclasses `ValueError` for exactly this, and
    `connector_app` replaces every *other* exception with a generic notice.
    """
    async with _session(running_server) as session:
        result = await session.call_tool("predict_pka", {"smiles": "C1CCNCC1"})
        assert result.isError is True
        assert "Spearman -0.17" in str(result.content)


async def test_an_unparameterised_solvent_is_refused_by_name(running_server: str) -> None:
    """The measured case is 2-MeTHF, and the refusal has to carry the alternative.

    Chemclaw3 caught this in a durable job's *precondition*, before a workflow started. There are no
    durable jobs here, so the check moved into `XtbSpec`'s validator — and this is where that move
    is verified to still produce a message the model can act on rather than tblite's "String value
    for epsilon was not found among database of solvents".
    """
    async with _session(running_server) as session:
        result = await session.call_tool(
            "compute_electronic_properties",
            {"smiles": "CCO", "solvent": "2-methyltetrahydrofuran"},
        )
        assert result.isError is True
        assert "tetrahydrofuran" in str(result.content)
