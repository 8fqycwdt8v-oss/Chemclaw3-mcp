"""`connector_app` as a caller meets it: a real socket, a real MCP session, a real refusal.

Covers what an in-process call cannot see:

- **The body cap's counting half**, driven by a chunked upload through the whole middleware
  stack (a `Content-Length` request only exercises the declared pre-check).
- **The error sanitiser's exemption**, which reads `ToolError.__cause__` as set by upstream's
  tool manager, so upstream must raise the errors.
- **What `/metrics` actually publishes**, asserted on the exposition.

The probe capability is deliberately tiny.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp_server_kit import Dataset, limits
from mcp_server_kit.app import connector_app
from prometheus_client.parser import text_string_to_metric_families

TOKEN = "test-token-for-the-kit"
TOKEN_ENV = "MCP_KIT_PROBE_TOKEN"
# Small enough that an oversize body is cheap to send, large enough that a real MCP handshake and
# tool call fit under it comfortably.
MAX_BYTES = 8_192
# Long enough that "time to SSE headers" and "time to the answer" cannot be confused for each
# other, short enough not to lengthen the suite noticeably. The defect this pins reported 1.1 ms
# for a 23.49 s call, so any threshold in between would do; this is a hundredfold margin.
SLOW_TOOL_SECONDS = 0.5

# What one real call supplies, written out so the exposition can be asked about these exact strings
# rather than about the words that happen to name them today. Every one is something a caller
# controls: three identity headers and a tool argument.
ACTOR = "alice@example.test"
SESSION = "session-7f3a91c4"
CORRELATION = "4bf92f3577b34da6a3ce929d0e0e4736"
CALLER_ARGUMENT = "CC(=O)Oc1ccccc1C(=O)O"


def _probe_server() -> FastMCP:
    """A FastMCP carrying one healthy tool and two that fail in the two ways that differ."""
    server = FastMCP("probe")

    @server.tool()
    def echo(text: str) -> str:
        """Return what was passed in."""
        return text

    @server.tool()
    def boom_internal() -> str:
        """Raise the kind of fault whose text must never reach the model."""
        raise RuntimeError("PGPASSWORD=hunter2 at postgres.internal:5432")

    @server.tool()
    def boom_domain() -> str:
        """Raise the kind of refusal that is the whole content of the answer."""
        raise ValueError("unknown solvent 'unobtainium'; see the vendored solvent table")

    @server.tool()
    def boom_domain_with_secret() -> str:
        """A caller-safe `ValueError` whose text happens to carry a secret — e.g. a validation

        message quoting an input that was a connection string. `UnicodeDecodeError`,
        `JSONDecodeError` and pydantic `ValidationError` are all `ValueError`s and routinely echo
        the offending input, so the caller-safe branch must redact.
        """
        raise ValueError("could not parse config PGPASSWORD=hunter2 at postgres.internal:5432")

    @server.tool()
    async def slow() -> str:
        """Take measurably longer than a round trip, so a duration can be read off a log line."""
        await asyncio.sleep(SLOW_TOOL_SECONDS)
        return "done"

    return server


@pytest.fixture(scope="module")
def running_server(serving: Callable[..., Any]) -> Iterator[str]:
    """The probe capability under uvicorn on loopback, wrapped by the real `connector_app`.

    Waits for the 200 so a startup failure shows here, not in whichever test ran first.
    """
    os.environ[TOKEN_ENV] = TOKEN
    app = connector_app(
        _probe_server(), name="probe", token_env=TOKEN_ENV, max_request_bytes=MAX_BYTES
    )
    with serving(app, require_ready=True) as base:
        yield base


def test_readiness_failure_is_a_503_naming_the_reason(serving: Callable[..., Any]) -> None:
    """A raising readiness callable makes a live `/healthz` answer 503 naming the reason.

    Without it a pod whose corpus failed its checksum passes the probe and fails every call; this is
    the one place every server's readiness check is the same code.
    """

    def _broken() -> list[Dataset]:
        raise RuntimeError("PGPASSWORD=hunter2 could not verify the solvent table")

    app = connector_app(_probe_server(), name="probe-unready", readiness=_broken)
    with serving(app) as base:
        response = httpx.get(f"{base}/healthz", timeout=5.0)
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "unready"
        assert body["server"] == "probe-unready"
        assert "could not verify the solvent table" in body["reason"]
        # `/healthz` is unauthenticated, so a secret named by the readiness exception must be
        # redacted.
        assert "hunter2" not in response.text
        assert "PGPASSWORD=***" in body["reason"]

        exposition = httpx.get(f"{base}/metrics", timeout=5.0).text
        assert 'chemclaw_mcp_ready{server="probe-unready"} 0.0' in exposition


def test_readiness_success_names_the_verified_datasets(serving: Callable[..., Any]) -> None:
    """The companion path: a `readiness` that succeeds is what `/healthz` reports back."""
    verified = Dataset(
        name="solvent-table",
        version="3",
        licence="CC0",
        retrieved_from="internal",
        description="probe",
        sha256="0" * 64,
        records_path=Path("/dev/null"),
    )

    app = connector_app(_probe_server(), name="probe-ready", readiness=lambda: [verified])
    with serving(app) as base:
        response = httpx.get(f"{base}/healthz", timeout=5.0)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["datasets"] == ["solvent-table@3"]

        exposition = httpx.get(f"{base}/metrics", timeout=5.0).text
        assert 'chemclaw_mcp_ready{server="probe-ready"} 1.0' in exposition


def test_healthz_reports_the_version_its_manifest_declares(
    serving: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`contract_version` is read from the packaged manifest of the server's own name.

    It rides on the ready answer and the unready one, and is absent where the manifest declares
    none or the name has no packaged manifest, so a server cannot report a version it was not given.
    """
    declared = {"probe-declares": "2.3.4", "probe-silent": None}

    def _packaged(name: str) -> str | None:
        return declared[name]  # a KeyError is "no packaged manifest", as in the real lookup

    monkeypatch.setattr("mcp_server_kit.app.contract_version", _packaged)

    def _broken() -> list[Dataset]:
        raise RuntimeError("corpus missing")

    for name, readiness, expected_status in (
        ("probe-declares", None, 200),
        ("probe-declares", _broken, 503),
        ("probe-silent", None, 200),
        ("probe-unpackaged", None, 200),
    ):
        app = connector_app(_probe_server(), name=name, readiness=readiness)
        with serving(app) as base:
            response = httpx.get(f"{base}/healthz", timeout=5.0)
            assert response.status_code == expected_status
            assert response.json().get("contract_version") == declared.get(name), name


@pytest.fixture
def isolated_bounds() -> Iterator[None]:
    """The process-wide bound record, restored after the test that writes to it."""
    saved = dict(limits._EFFECTIVE)
    yield
    limits._EFFECTIVE.clear()
    limits._EFFECTIVE.update(saved)


def test_healthz_reports_the_bounds_the_process_is_running_with(
    serving: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, isolated_bounds: None
) -> None:
    """An operator's override is read off the probe, not inferred from the image.

    The deployment ratchets read shipped files and cannot see an overlay or `kubectl set env`. A
    server bound, the session ceiling and the thread-pool width are each moved by the environment
    and read back from a live `/healthz`, on the unready answer too.
    """
    monkeypatch.setenv("CHEMCLAW_PROBE_MAX_WIDGETS", "9")
    monkeypatch.setenv("MCP_MAX_SESSIONS", "7")
    monkeypatch.setenv("MCP_THREAD_POOL_SIZE", "3")
    assert (
        limits.env_bound("CHEMCLAW_PROBE_MAX_WIDGETS", default=4, minimum=1, consequence="none")
        == 9
    )

    def _broken() -> list[Dataset]:
        raise RuntimeError("the probe's corpus is missing")

    for readiness, status in ((None, 200), (_broken, 503)):
        app = connector_app(_probe_server(), name="probe-bounds", readiness=readiness)
        with serving(app) as base:
            response = httpx.get(f"{base}/healthz", timeout=5.0)
        assert response.status_code == status
        bounds = response.json()["bounds"]
        assert bounds["CHEMCLAW_PROBE_MAX_WIDGETS"] == 9, "an env_bound override is not reported"
        assert bounds["MCP_MAX_SESSIONS"] == 7, "the session ceiling is not reported"
        assert bounds["MCP_THREAD_POOL_SIZE"] == 3, "the installed pool width is not reported"
        # The kit's own module-level bounds, read at import with nothing set.
        assert bounds["MCP_MAX_MOLECULE_ATOMS"] == limits.MAX_MOLECULE_ATOMS
        assert list(bounds) == sorted(bounds), "the record is reported in a stable order"


def test_a_declared_oversize_body_is_refused(running_server: str) -> None:
    """The half that already worked: a `content-length` over the cap never reaches a handler."""
    response = httpx.post(
        f"{running_server}/mcp",
        headers={"authorization": f"Bearer {TOKEN}"},
        content=b"x" * (MAX_BYTES * 2),
        timeout=10.0,
    )
    assert response.status_code == 413
    assert response.text == "request body too large"


def test_a_chunked_oversize_body_is_refused_with_413_and_not_a_500(running_server: str) -> None:
    """A chunked oversize body is refused with 413, never a 500.

    A chunked upload declares no `content-length`, so the running counter is its only bound; a 500
    with a traceback per request would let any caller inflate log volume. Asserted as 413 and as
    not-500.
    """

    def chunks() -> Iterator[bytes]:
        for _ in range(8):
            yield b"x" * MAX_BYTES

    response = httpx.post(
        f"{running_server}/mcp",
        headers={"authorization": f"Bearer {TOKEN}"},
        content=chunks(),
        timeout=10.0,
    )
    assert response.status_code == 413, f"chunked oversize answered {response.status_code}"
    assert response.text == "request body too large"


def test_a_body_under_the_cap_is_served_chunked_too(running_server: str) -> None:
    """The cap must not refuse a legitimate streamed request — otherwise 413 proves nothing."""

    def chunks() -> Iterator[bytes]:
        yield b'{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}'

    response = httpx.post(
        f"{running_server}/mcp",
        headers={
            "authorization": f"Bearer {TOKEN}",
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
        },
        content=chunks(),
        timeout=10.0,
    )
    assert response.status_code != 413


async def test_an_unknown_tool_name_is_named_back_to_the_caller(
    running_server: str, mcp_session: Callable[..., Any]
) -> None:
    """Upstream's `Unknown tool: x` is a caller-safe message and must survive the sanitiser.

    It is raised without a cause, so it must not be treated as an internal fault: the model needs
    the name to correct itself, and a client error must not log a stack trace at ERROR.
    """
    async with mcp_session(running_server, token=TOKEN) as session:
        result = await session.call_tool("no_such_tool", {})
        assert result.isError is True
        assert "no_such_tool" in str(result.content)


async def test_an_internal_fault_is_still_replaced(
    running_server: str, mcp_session: Callable[..., Any]
) -> None:
    """The exemption must not widen: a `RuntimeError`'s text still never reaches the model."""
    async with mcp_session(running_server, token=TOKEN) as session:
        result = await session.call_tool("boom_internal", {})
        assert result.isError is True
        assert "an internal error occurred" in str(result.content)
        assert "PGPASSWORD" not in str(result.content)


async def test_a_domain_refusal_still_passes_through(
    running_server: str, mcp_session: Callable[..., Any]
) -> None:
    """And a deliberately worded `ValueError` is still the whole content of the answer."""
    async with mcp_session(running_server, token=TOKEN) as session:
        result = await session.call_tool("boom_domain", {})
        assert result.isError is True
        assert "unobtainium" in str(result.content)


async def test_a_caller_safe_message_is_redacted_before_the_model_sees_it(
    running_server: str,
    mcp_session: Callable[..., Any],
) -> None:
    """A `ValueError` passes through, but not with a secret in it.

    `ValueError` subclasses that echo their input (pydantic, decode errors) could otherwise leak a
    mounted secret; the same `redact_secrets` as the readiness path applies, and the worded text
    survives.
    """
    async with mcp_session(running_server, token=TOKEN) as session:
        result = await session.call_tool("boom_domain_with_secret", {})
        assert result.isError is True
        content = str(result.content)
        assert "hunter2" not in content
        assert "PGPASSWORD=***" in content
        # The message is still the worded refusal, not the generic internal-error notice.
        assert "an internal error occurred" not in content
        assert "could not parse config" in content


def test_upstream_still_chains_a_tool_fault_and_still_does_not_chain_unknown_tool() -> None:
    """Pins the upstream property the sanitiser's exemption reads.

    Upstream raises `ToolError(...) from e` for a tool fault and a bare `ToolError` for an unknown
    name; neither is promised, so a change upstream turns red here instead of leaking fault text.
    """
    import inspect

    from mcp.server.fastmcp.tools import base, tool_manager

    assert 'raise ToolError(f"Error executing tool {self.name}: {e}") from e' in inspect.getsource(
        base.Tool.run
    )
    source = inspect.getsource(tool_manager.ToolManager.call_tool)
    assert 'raise ToolError(f"Unknown tool: {name}")' in source
    assert "from" not in source.split("Unknown tool")[1].split("\n")[0]


async def test_metrics_is_open_and_carries_no_identity(
    running_server: str, mcp_session: Callable[..., Any]
) -> None:
    """`/metrics` is unauthenticated on purpose, so what it may carry is the whole control.

    It publishes the default registry's process metrics, but no request content, caller identity or
    tool argument. A real call carries real identity through the stack, and the strings it sent are
    asserted absent as label values, whatever a label is called.
    """
    identity = {
        "X-Chemclaw-Actor": ACTOR,
        "X-Chemclaw-Session": SESSION,
        "X-Chemclaw-Correlation-Id": CORRELATION,
        "X-Chemclaw-Dry-Run": "true",
    }
    async with mcp_session(running_server, token=TOKEN, headers=identity) as session:
        await session.call_tool("echo", {"text": CALLER_ARGUMENT})

    response = httpx.get(f"{running_server}/metrics", timeout=5.0)  # noqa: ASYNC210
    assert response.status_code == 200
    exposition = response.text
    for supplied in (ACTOR, SESSION, CORRELATION, CALLER_ARGUMENT, TOKEN):
        assert supplied not in exposition, (
            f"/metrics is unauthenticated and published {supplied!r}, which the caller supplied; a "
            "labelled metric on this endpoint must never take an actor, a session, a correlation "
            "id or a tool argument as a label, whatever that label is named"
        )
    # Parse label *names* rather than substring-scan: the process registry holds other servers' tool
    # names as values, and e.g. `continuous_reactor_conversion` contains "actor".
    label_names = {
        name.lower()
        for family in text_string_to_metric_families(exposition)
        for sample in family.samples
        for name in sample.labels
    }
    for forbidden in ("actor", "session", "correlation", "smiles", "authorization", "bearer"):
        named = sorted(name for name in label_names if forbidden in name)
        assert not named, (
            f"/metrics is unauthenticated and labels a series by {named}; a labelled metric on "
            "this endpoint must never carry an actor, a session, a correlation id or a tool "
            "argument"
        )


def test_the_egress_counter_is_unlabelled(running_server: str) -> None:
    """The destination host of a refused connection is attacker-influenced, so it is not a label.

    A label would mint a series per name anything could make the pod resolve.
    """
    exposition = httpx.get(f"{running_server}/metrics", timeout=5.0).text
    lines = [
        line
        for line in exposition.splitlines()
        if line.startswith("chemclaw_mcp_egress_refused_total")
    ]
    assert lines, "the egress counter is not published at all"
    for line in lines:
        assert "{" not in line, (
            f"chemclaw_mcp_egress_refused_total carries a label ({line!r}); the only thing there "
            "is to label it with is the destination host, which a caller influences and nothing "
            "bounds"
        )


async def test_metrics_publishes_what_a_tool_call_did(
    running_server: str, mcp_session: Callable[..., Any]
) -> None:
    """`/metrics` publishes per-tool calls for every outcome, so it is not merely free of identity.

    An empty exposition passes every absence rule. Outcomes are driven through the real transport
    because `outcome` is decided by upstream's `ToolError.__cause__`.
    """
    async with mcp_session(running_server, token=TOKEN) as session:
        await session.call_tool("echo", {"text": "hello"})
        await session.call_tool("boom_domain", {})
        await session.call_tool("boom_internal", {})

    exposition = httpx.get(f"{running_server}/metrics", timeout=5.0).text  # noqa: ASYNC210
    for expected in (
        'chemclaw_mcp_tool_calls_total{outcome="ok",server="probe",tool="echo"}',
        'chemclaw_mcp_tool_calls_total{outcome="refused",server="probe",tool="boom_domain"}',
        'chemclaw_mcp_tool_calls_total{outcome="failed",server="probe",tool="boom_internal"}',
        'chemclaw_mcp_tool_duration_seconds_count{server="probe",tool="echo"}',
        'chemclaw_mcp_requests_total{path="/mcp",server="probe",status="200"}',
        "chemclaw_mcp_build_info{revision=",
        "chemclaw_mcp_egress_guard_armed 1.0",
    ):
        assert expected in exposition, f"/metrics does not publish {expected!r}"


async def test_an_unknown_tool_name_cannot_mint_a_metric_series(
    running_server: str, mcp_session: Callable[..., Any]
) -> None:
    """An unknown tool name is counted under `<unknown>`, never as its own series.

    The name is caller-supplied, so an unclamped label would mint one series per invented string.
    The call is still counted, since guessing tool names is a signal.
    """
    async with mcp_session(running_server, token=TOKEN) as session:
        result = await session.call_tool("definitely_not_a_tool_here", {})
        assert result.isError is True

    exposition = httpx.get(f"{running_server}/metrics", timeout=5.0).text  # noqa: ASYNC210
    assert "definitely_not_a_tool_here" not in exposition, (
        "a caller-supplied tool name reached a metric label; the label set is then unbounded and "
        "anything that can reach the pod can grow it until the process runs out of memory"
    )
    assert 'chemclaw_mcp_tool_calls_total{outcome="refused",server="probe",tool="<unknown>"}' in (
        exposition
    ), "an unknown tool name must still be counted, under the sentinel"


def test_metrics_counts_the_requests_it_refused(running_server: str) -> None:
    """A counter whose help says "HTTP requests served" must see the ones nothing served.

    The credential check and body cap short-circuit before the tool layer; without them in the count
    `rate(...{status=~"4.."})` is always 0. Both refusals are driven over a real socket.
    """
    for _ in range(3):
        refused = httpx.post(f"{running_server}/mcp", content=b"{}", timeout=10.0)
        assert refused.status_code == 401
    oversize = httpx.post(
        f"{running_server}/mcp",
        headers={"authorization": f"Bearer {TOKEN}"},
        content=b"x" * (MAX_BYTES * 2),
        timeout=10.0,
    )
    assert oversize.status_code == 413

    exposition = httpx.get(f"{running_server}/metrics", timeout=5.0).text
    for expected in (
        'chemclaw_mcp_requests_total{path="/mcp",server="probe",status="401"}',
        'chemclaw_mcp_requests_total{path="/mcp",server="probe",status="413"}',
    ):
        assert expected in exposition, (
            f"/metrics does not publish {expected!r}; the request counter cannot see the requests "
            "the layers outside it refuse"
        )


def test_connector_app_refuses_to_wrap_one_capability_twice() -> None:
    """Wrapping is not idempotent, so a second call must be an error rather than a double count.

    Each wrapper captures the previous `call_tool`, so wrapping twice would count every call twice.
    """
    server = _probe_server()
    connector_app(server, name="twice-probe")
    with pytest.raises(RuntimeError, match="already wrapped"):
        connector_app(server, name="twice-probe")


class _Keeper(logging.Handler):
    """A root handler that keeps every record, added *after* the app has configured logging.

    `configure_logging()` runs from the lifespan with `force=True` and would remove a handler
    installed before startup — which is the same trap `test_logging.py` records, one layer out.
    """

    def __init__(self) -> None:
        """Start empty, at DEBUG, so nothing is filtered before the test can see it."""
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Keep the record rather than writing it anywhere."""
        self.records.append(record)


async def test_the_request_log_measures_the_whole_call_and_not_the_sse_headers(
    running_server: str, mcp_session: Callable[..., Any]
) -> None:
    """The request line's `duration_ms` covers the whole call, not just the SSE headers.

    A tool call is an SSE stream whose body is written after the response starts, so logging at
    response start reports time-to-headers. Driven over a real socket with a tool that sleeps.
    """
    keeper = _Keeper()
    root = logging.getLogger()
    root.addHandler(keeper)
    try:
        async with mcp_session(running_server, token=TOKEN) as session:
            result = await session.call_tool("slow", {})
        assert not result.isError
    finally:
        root.removeHandler(keeper)

    lines = [
        record.getMessage()
        for record in keeper.records
        if "request: path=/mcp" in record.getMessage()
    ]
    assert lines, "no request line was logged for the tool call"
    durations = [
        float(found.group(1))
        for line in lines
        if (found := re.search(r"duration_ms=([\d.]+)", line))
    ]
    assert max(durations) >= SLOW_TOOL_SECONDS * 1000 * 0.8, (
        f"the longest request logged {max(durations):.1f} ms for a call that took at least "
        f"{SLOW_TOOL_SECONDS * 1000:.0f} ms; duration_ms is measuring time-to-headers again"
    )


def test_a_probe_path_with_a_trailing_slash_answers(running_server: str) -> None:
    """`/healthz/` and `/metrics/` must *answer*, not merely escape the credential check.

    The MCP mount at `/` swallows Starlette's redirect, so without an alias these return 404.
    Redirects are not followed: kubelet counts a 3xx as a passing probe, so a redirect is not an
    answer.
    """
    healthz = httpx.get(f"{running_server}/healthz/", timeout=5.0, follow_redirects=False)
    assert healthz.status_code == 200, "a trailing-slash probe path must answer the probe itself"
    assert healthz.json()["status"] == "ok"
    assert healthz.json()["server"] == "probe"

    metrics = httpx.get(f"{running_server}/metrics/", timeout=5.0, follow_redirects=False)
    assert metrics.status_code == 200
    assert "chemclaw_mcp_build_info" in metrics.text


def test_a_trailing_slash_probe_still_reports_unreadiness(
    serving: Callable[..., Any],
) -> None:
    """The alias serves the route rather than pointing at it, which is why a 503 survives.

    Kubelet treats any 2xx or 3xx as passing, so a redirect would report an unready pod as ready.
    """

    def _broken() -> list[Dataset]:
        raise RuntimeError("could not verify the solvent table")

    app = connector_app(_probe_server(), name="probe-unready-slash", readiness=_broken)
    # A raising readiness callable never answers 200, so wait only for the port to accept.
    with serving(app, require_ready=False) as base:
        response = httpx.get(f"{base}/healthz/", timeout=5.0, follow_redirects=False)
        assert response.status_code == 503
        assert response.json()["status"] == "unready"
