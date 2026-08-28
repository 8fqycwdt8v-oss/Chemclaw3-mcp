"""The `traceparent` Chemclaw3 sends must produce a span here, under the caller's trace.

Chemclaw3 injects W3C trace context on every connector call and its own tracing docstring states the
consequence in the present tense: "a connector's work appears inside the turn that asked for it".
For this fleet that was false. The header arrived on every request, `connector_app` read the four
`X-Chemclaw-*` headers beside it, and the trace context was dropped — no span was created on this
side at all, so the expensive half of a chemist's question was invisible to whatever the collector
showed.

**The assertion is the join, not the call.** A test that only checked "a span exists" would pass
against an implementation that starts a fresh root trace per call, which is precisely the state this
fixes: the value is entirely in the *parenting*. So the incoming ids are literals here, and the
recorded span's `trace_id` and `parent.span_id` are compared against them.

Everything runs against a real socket, a real MCP session and the real `connector_app`, for the
reason `test_connector_app.py` gives: the behaviour under test lives in the request the *tool call*
is serving, and an in-process call has no such request.

**The exporter is in memory and there is no other kind here.** The egress guard is armed for this
run like every other, so an OTLP exporter would raise `EgressForbidden` — the guard working.
Nothing in this workspace may dial a collector; a deployment that wants one configures the SDK
itself.
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


def _probe_server() -> FastMCP:
    """One tool, because this file is about the transport's span and not about any chemistry."""
    server = FastMCP("tracing-probe")

    @server.tool()
    def echo(text: str) -> str:
        """Return what was passed in."""
        return text

    return server


def _free_port() -> int:
    """An ephemeral loopback port, released immediately for uvicorn to claim."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module", autouse=True)
def recording_provider() -> Iterator[None]:
    """Make the global tracer provider record into memory.

    Global because `tracing.py` asks `opentelemetry.trace` for a tracer, which is what a
    deployment's own SDK bootstrap configures — passing a provider in would test a seam that does
    not exist.
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

    The transport's own client is yielded beside the session because one of the properties under
    test is *per call* rather than per session: the headers are fixed at construction here, exactly
    as they are for a real long-lived connection, so a test that wants a second call to carry a
    second trace has to reach that client and change them between calls.
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
    """The property `_continue_trace_per_tool_call` exists for, and the only test that can see it.

    Every other test here opens a session, sends one call, and closes it — so the handshake's
    `traceparent` and the call's are the same string, and an implementation that extracted the
    context once in ASGI middleware (the obvious simplification: it deletes a wrapper and reads the
    request where it is naturally available) passes all of them. Measured against exactly that
    implementation: the four tests stayed green and the whole workspace suite stayed green, while
    every call on a long-lived session was reported inside whichever turn opened the connection.

    One MCP session carries many turns, so this sends two calls down one session with two different
    traces and asserts each span landed under its own. The direct analogue of
    `tests/test_identity_contract.py`'s two-callers-one-session test, for the same reason: the
    client fixes its headers at construction and the thing under test is per-call.
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

    The tool name in a `tools/call` is caller-supplied and reaches `ToolManager.call_tool`
    unvalidated. `_instrument_tool_calls` folds anything unserved into `UNKNOWN_TOOL` before it
    becomes a Prometheus label; this asserts the span does the same, which it did not — it took
    the string verbatim into both the span name and the `mcp.tool` attribute, so a model retrying
    a stale name minted one operation name per string in whatever collector receives the spans.
    Nothing accumulates in this process, which is why the two halves of one rule diverged quietly.
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
