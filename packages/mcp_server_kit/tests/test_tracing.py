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
async def _session(base: str, *, traceparent: str | None) -> AsyncIterator[ClientSession]:
    """An initialised MCP session carrying the bearer token, and optionally a caller's trace."""
    headers = {"Authorization": f"Bearer {TOKEN}"}
    if traceparent is not None:
        headers["traceparent"] = traceparent
    async with (
        httpx.AsyncClient(headers=headers) as http_client,
        streamable_http_client(f"{base}/mcp", http_client=http_client) as (rx, tx, _),
        ClientSession(rx, tx) as session,
    ):
        await session.initialize()
        yield session


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
    async with _session(running_server, traceparent=TRACEPARENT) as session:
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


async def test_the_span_names_the_server_and_the_tool_and_nothing_about_the_caller(
    running_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A span attribute travels to a collector, so it follows `/metrics`' rule: identifiers only.

    The `X-Chemclaw-*` headers arrive on the very same request and must not be attached: an actor on
    a span publishes per-actor call volumes to whatever the collector shows.
    """
    monkeypatch.setenv(TRACING_ENABLED_ENV, "1")
    async with _session(running_server, traceparent=TRACEPARENT) as session:
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
    async with _session(running_server, traceparent=None) as session:
        await session.call_tool("echo", {"text": "hello"})

    span = next(s for s in EXPORTER.get_finished_spans() if s.name == "mcp.tool/echo")
    assert span.parent is None


async def test_tracing_is_off_unless_a_deployment_turns_it_on(
    running_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default, and the one that matters for the egress posture: no span, no SDK work at all."""
    monkeypatch.delenv(TRACING_ENABLED_ENV, raising=False)
    async with _session(running_server, traceparent=TRACEPARENT) as session:
        result = await session.call_tool("echo", {"text": "hello"})

    assert not result.isError
    assert EXPORTER.get_finished_spans() == ()
