"""The `traceparent` Chemclaw3 sends must produce a span here, under the caller's trace.

The assertion is the join: incoming ids are literals and the recorded span's `trace_id` and
`parent.span_id` are compared against them, since a fresh root trace per call would also "create
a span".

Runs over a real socket, MCP session and `connector_app`, because the behaviour lives in the
request a tool call serves. The exporter is in memory; the armed egress guard would refuse OTLP.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP
from mcp_server_kit.app import connector_app
from mcp_server_kit.metrics import UNKNOWN_TOOL
from mcp_server_kit.tracing import TRACING_ENABLED_ENV
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

TOKEN = "test-token-for-tracing"
TOKEN_ENV = "MCP_KIT_TRACING_TOKEN"

# A caller's trace, written out rather than generated, so the assertions below compare against
# numbers a reader can see in the header string.
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_SPAN_ID = "00f067aa0ba902b7"
TRACEPARENT = f"00-{TRACE_ID}-{PARENT_SPAN_ID}-01"

# A *second* caller's trace, for the test that sends two calls down one MCP session. Different in
# both halves, because a per-handshake implementation reproduces the first call's ids exactly.
SECOND_TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
SECOND_PARENT_SPAN_ID = "b7ad6b7169203331"
SECOND_TRACEPARENT = f"00-{SECOND_TRACE_ID}-{SECOND_PARENT_SPAN_ID}-01"

EXPORTER = InMemorySpanExporter()


# The two strings the leak measurement looks for, written out so a reader can see exactly what must
# not reach a collector: a credential the *log* path redacts correctly, and a caller's own argument.
SECRET = "hunter2"
CALLER_ARGUMENT = "CCO-secret-molecule"


def _probe_server() -> FastMCP:
    """Three tools: one that answers, and the two failures whose span records differ."""
    server = FastMCP("tracing-probe")

    @server.tool()
    def echo(text: str) -> str:
        """Return what was passed in."""
        return text

    @server.tool()
    def boom_internal() -> str:
        """Raise the kind of fault whose text must never leave this process."""
        raise RuntimeError(f"PGPASSWORD={SECRET} at postgres.internal:5432")

    @server.tool()
    def boom_domain() -> str:
        """Raise the kind of refusal that is the whole content of a correct answer."""
        raise ValueError(f"unknown solvent {CALLER_ARGUMENT!r}; see the vendored solvent table")

    return server


def _span_text(span: object) -> str:
    """Every string a collector would receive for `span`, concatenated.

    One blob so assertions about a value never leaving cover attributes added later.
    """
    parts = [
        str(getattr(span, "name", "")),
        str(getattr(getattr(span, "status", None), "description", "")),
    ]
    for key, value in dict(getattr(span, "attributes", None) or {}).items():
        parts.append(f"{key}={value}")
    for event in getattr(span, "events", ()) or ():
        parts.append(event.name)
        for key, value in dict(event.attributes or {}).items():
            parts.append(f"{key}={value}")
    return "".join(parts)


def _free_port() -> int:
    """An ephemeral loopback port, released immediately for uvicorn to claim."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module", autouse=True)
def recording_provider() -> Iterator[None]:
    """Make the global tracer provider record into memory.

    Global because `tracing.py` asks `opentelemetry.trace` for a tracer, as a deployment's SDK
    bootstrap configures it.
    """
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
    trace.set_tracer_provider(provider)
    yield
    provider.shutdown()


@pytest.fixture(scope="module")
def running_server() -> Iterator[str]:
    """The probe capability under uvicorn on loopback, wrapped by the real `connector_app`."""
    os.environ[TOKEN_ENV] = TOKEN
    app = connector_app(_probe_server(), name="tracing-probe", token_env=TOKEN_ENV)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
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
        pytest.fail("the tracing probe server did not become ready within 30 s")
    yield base
    server.should_exit = True
    thread.join(timeout=10)


@asynccontextmanager
async def _session(
    base: str, *, traceparent: str | None
) -> AsyncIterator[tuple[ClientSession, httpx.AsyncClient]]:
    """An initialised MCP session carrying the bearer token, and optionally a caller's trace.

    The transport's client is yielded too, so a test can change its fixed headers between calls to
    send a second trace on the same session.
    """
    headers = {"Authorization": f"Bearer {TOKEN}"}
    if traceparent is not None:
        headers["traceparent"] = traceparent
    async with (
        httpx.AsyncClient(headers=headers) as http_client,
        streamable_http_client(f"{base}/mcp", http_client=http_client) as (rx, tx, _),
        ClientSession(rx, tx) as session,
    ):
        await session.initialize()
        yield session, http_client


@pytest.fixture(autouse=True)
def _clear_spans() -> Iterator[None]:
    """One test's spans must not be another's evidence."""
    EXPORTER.clear()
    yield
    EXPORTER.clear()


async def test_a_tool_call_becomes_a_span_under_the_caller_s_trace(
    running_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: the span is a *child* of the turn that called, not a fresh root trace."""
    monkeypatch.setenv(TRACING_ENABLED_ENV, "1")
    async with _session(running_server, traceparent=TRACEPARENT) as (session, _client):
        result = await session.call_tool("echo", {"text": "hello"})
    assert not result.isError

    spans = [span for span in EXPORTER.get_finished_spans() if span.name == "mcp.tool/echo"]
    assert spans, (
        "the tool call produced no span: `traceparent` arrived and was discarded, so this "
        "server's work is an orphan trace rather than part of the turn that asked for it"
    )
    span = spans[0]
    assert span.context is not None
    assert f"{span.context.trace_id:032x}" == TRACE_ID, (
        "the span started a new trace instead of continuing the caller's"
    )
    assert span.parent is not None
    assert f"{span.parent.span_id:016x}" == PARENT_SPAN_ID


async def test_each_call_on_one_session_is_parented_on_that_call_s_own_trace(
    running_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each call on one session is parented on that call's own trace.

    One session carries many turns, so extracting the context once at the handshake would put every
    call under the turn that opened the connection. Two calls with two traces on one session, each
    asserted under its own.
    """
    monkeypatch.setenv(TRACING_ENABLED_ENV, "1")
    async with _session(running_server, traceparent=TRACEPARENT) as (session, client):
        await session.call_tool("echo", {"text": "first"})
        client.headers["traceparent"] = SECOND_TRACEPARENT
        await session.call_tool("echo", {"text": "second"})

    spans = [span for span in EXPORTER.get_finished_spans() if span.name == "mcp.tool/echo"]
    assert len(spans) == 2, f"expected one span per call, got {len(spans)}"
    seen = [
        (f"{span.context.trace_id:032x}", f"{span.parent.span_id:016x}")
        for span in spans
        if span.context is not None and span.parent is not None
    ]
    assert seen == [
        (TRACE_ID, PARENT_SPAN_ID),
        (SECOND_TRACE_ID, SECOND_PARENT_SPAN_ID),
    ], (
        "the second call was reported under the first call's trace: the context is being taken "
        "from the handshake rather than from the request each tool call is serving, so every "
        "call on a long-lived session joins whichever turn happened to open the connection"
    )


async def test_the_span_names_the_server_and_the_tool_and_nothing_about_the_caller(
    running_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A span attribute travels to a collector, so it follows `/metrics`' rule: identifiers only.

    The `X-Chemclaw-*` headers arrive on the very same request and must not be attached: an actor on
    a span publishes per-actor call volumes to whatever the collector shows.
    """
    monkeypatch.setenv(TRACING_ENABLED_ENV, "1")
    async with _session(running_server, traceparent=TRACEPARENT) as (session, _client):
        await session.call_tool("echo", {"text": "hello"})

    span = next(s for s in EXPORTER.get_finished_spans() if s.name == "mcp.tool/echo")
    assert dict(span.attributes or {}) == {"mcp.server": "tracing-probe", "mcp.tool": "echo"}


async def test_a_call_with_no_trace_context_still_produces_a_span(
    running_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller that sends no `traceparent` gets a root span rather than an error or a skip.

    Chemclaw3 sends none when its own tracing is off, and a Temporal activity calling this server
    directly may send none either — the code path must be the one production takes.
    """
    monkeypatch.setenv(TRACING_ENABLED_ENV, "1")
    async with _session(running_server, traceparent=None) as (session, _client):
        await session.call_tool("echo", {"text": "hello"})

    span = next(s for s in EXPORTER.get_finished_spans() if s.name == "mcp.tool/echo")
    assert span.parent is None


async def test_tracing_is_off_unless_a_deployment_turns_it_on(
    running_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default, and the one that matters for the egress posture: no span, no SDK work at all."""
    monkeypatch.delenv(TRACING_ENABLED_ENV, raising=False)
    async with _session(running_server, traceparent=TRACEPARENT) as (session, _client):
        result = await session.call_tool("echo", {"text": "hello"})

    assert not result.isError
    assert EXPORTER.get_finished_spans() == ()


async def test_an_unknown_tool_name_cannot_mint_a_span_name(
    running_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A span name is an identifier that leaves the pod, so it takes the clamp `/metrics` takes.

    The tool name is caller-supplied; an unknown one becomes `UNKNOWN_TOOL` in the span name and the
    `mcp.tool` attribute, as in the metric.
    """
    monkeypatch.setenv(TRACING_ENABLED_ENV, "1")
    hostile = "definitely_not_a_tool_here"
    async with _session(running_server, traceparent=TRACEPARENT) as (session, _client):
        result = await session.call_tool(hostile, {})
    assert result.isError is True

    spans = EXPORTER.get_finished_spans()
    assert spans, "an unserved tool name must still produce a span; the call happened"
    assert not any(hostile in span.name for span in spans), (
        "a caller-supplied tool name reached a span name: operation cardinality in the collector "
        "is then unbounded by anything this server serves"
    )
    assert not any(hostile in str(dict(span.attributes or {})) for span in spans)
    assert [span.name for span in spans] == [f"mcp.tool/{UNKNOWN_TOOL}"]
    assert dict(spans[0].attributes or {}) == {
        "mcp.server": "tracing-probe",
        "mcp.tool": UNKNOWN_TOOL,
    }


async def test_a_fault_puts_neither_its_text_nor_its_stack_on_the_span(
    running_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fault puts neither its text nor its stack on the span.

    `record_exception=True` would walk the preserved cause chain into `exception.stacktrace` for a
    third-party collector. Asserted absent: the secret and the stack, since `tracing.py` promises
    identifiers only.
    """
    monkeypatch.setenv(TRACING_ENABLED_ENV, "1")
    async with _session(running_server, traceparent=TRACEPARENT) as (session, _client):
        result = await session.call_tool("boom_internal", {})
    assert result.isError is True

    span = next(s for s in EXPORTER.get_finished_spans() if s.name == "mcp.tool/boom_internal")
    blob = _span_text(span)
    assert SECRET not in blob, (
        "a credential reached a span; spans go to a collector this repository does not own, and "
        "this is the one path that passes neither the error sanitiser nor `redact_secrets`"
    )
    assert "Traceback" not in blob and "exception.stacktrace" not in blob
    assert span.events == ()


async def test_a_refusal_is_not_an_error_span_and_never_quotes_the_caller(
    running_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The span's outcome is the metric's, and a refusal is a correct answer in both.

    A refusal is not an ERROR span, and its domain message (which may hold a caller's molecule) is
    not recorded.
    """
    monkeypatch.setenv(TRACING_ENABLED_ENV, "1")
    async with _session(running_server, traceparent=TRACEPARENT) as (session, _client):
        result = await session.call_tool("boom_domain", {})
    assert result.isError is True
    assert CALLER_ARGUMENT in str(result.content), "the refusal must still reach the model in full"

    span = next(s for s in EXPORTER.get_finished_spans() if s.name == "mcp.tool/boom_domain")
    assert span.status.status_code is not StatusCode.ERROR, (
        "a refusal is the answer the caller asked for; an ERROR span here contradicts the "
        '`outcome="refused"` the counter books for the same call'
    )
    assert CALLER_ARGUMENT not in _span_text(span)


async def test_a_hostile_tool_name_cannot_become_a_span_name(
    running_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hostile tool name cannot become a span name; it is clamped as the metric label is."""
    monkeypatch.setenv(TRACING_ENABLED_ENV, "1")
    hostile = "../../etc/passwd?a=b"
    async with _session(running_server, traceparent=TRACEPARENT) as (session, _client):
        await session.call_tool(hostile, {})
        await session.call_tool("N" * 309, {})

    names = [span.name for span in EXPORTER.get_finished_spans()]
    assert names == ["mcp.tool/<unknown>", "mcp.tool/<unknown>"], names
    assert not any(hostile in name for name in names)
