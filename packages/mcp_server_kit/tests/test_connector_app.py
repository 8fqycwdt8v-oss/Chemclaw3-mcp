"""`connector_app` as a caller meets it: a real socket, a real MCP session, a real refusal.

Every other test in this package drives a function or an in-process ASGI app. Three of the
behaviours `connector_app` promises cannot be seen that way, and each shipped broken *because* the
only tests were the in-process ones:

- **The body cap's counting half.** Its one test sent `content=b"x" * 4096`, which httpx sends with
  a `Content-Length` — so it exercised the declared pre-check and never the counter. Over a real
  socket, with the two `BaseHTTPMiddleware` layers `connector_app` installs in between, the
  counter's signal arrived at the cap as a nested `ExceptionGroup` and became a 500 with a
  per-request traceback. The class's own docstring says it was fixed once for exactly this — "the
  test for it passed for the wrong reason" — which is why the replacement drives a chunked upload
  through the whole stack rather than the middleware alone.
- **The error sanitiser's exemption.** It is written against `ToolError.__cause__`, a property of
  the *upstream* tool manager, and the only way to know which of upstream's `ToolError`s carry a
  cause is to make upstream raise them.
- **What `/metrics` actually publishes.** The claim is about the exposition, not about the code
  that generates it.

The probe capability is deliberately tiny: this file is about the shape every server shares, not
about any server's chemistry.
"""

from __future__ import annotations

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

TOKEN = "test-token-for-the-kit"
TOKEN_ENV = "MCP_KIT_PROBE_TOKEN"
# Small enough that an oversize body is cheap to send, large enough that a real MCP handshake and
# tool call fit under it comfortably.
MAX_BYTES = 8_192


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

    return server


def _free_port() -> int:
    """An ephemeral loopback port, released immediately for uvicorn to claim."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def running_server() -> Iterator[str]:
    """The probe capability under uvicorn on loopback, wrapped by the real `connector_app`."""
    import os

    os.environ[TOKEN_ENV] = TOKEN
    app = connector_app(
        _probe_server(), name="probe", token_env=TOKEN_ENV, max_request_bytes=MAX_BYTES
    )
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
        pytest.fail("the probe server did not become ready within 30 s")
    yield base
    server.should_exit = True
    thread.join(timeout=10)


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
    """The half that never worked, and the reason the cap has a counter at all.

    A chunked upload declares no `content-length`, so the running total is the only thing that
    bounds it. Measured before the fix, against this exact stack: **500 Internal Server Error**
    plus a ~40-line nested `ExceptionGroup` traceback per request — so anything that could reach
    the pod could turn a size refusal into unbounded log volume, and an operator's dashboard
    showed unhandled server errors rather than rejected oversize requests.

    Sent with a body iterator so httpx uses `Transfer-Encoding: chunked`; asserted as an absence
    of 500 as well as a presence of 413, because "some error happened" is what it did before.
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


async def test_an_unknown_tool_name_is_named_back_to_the_caller(running_server: str) -> None:
    """Upstream's `Unknown tool: x` is a caller-safe message and must survive the sanitiser.

    `ToolManager.call_tool` raises it with no `from`, so it reached the sanitiser looking exactly
    like an internal fault: the model was told "an internal error occurred" — which gives it
    nothing to correct — and `logger.exception` fired at ERROR with a stack trace for what is a
    client input error, diluting the signal the sanitiser exists to preserve.

    This is the repository's own "refuse rather than approximate" rule applied to the transport:
    an unknown solvent is an error naming the corpus, so an unknown tool is an error naming the
    tool.
    """
    async with _session(running_server) as session:
        result = await session.call_tool("no_such_tool", {})
        assert result.isError is True
        assert "no_such_tool" in str(result.content)


async def test_an_internal_fault_is_still_replaced(running_server: str) -> None:
    """The exemption must not widen: a `RuntimeError`'s text still never reaches the model."""
    async with _session(running_server) as session:
        result = await session.call_tool("boom_internal", {})
        assert result.isError is True
        assert "an internal error occurred" in str(result.content)
        assert "PGPASSWORD" not in str(result.content)


async def test_a_domain_refusal_still_passes_through(running_server: str) -> None:
    """And a deliberately worded `ValueError` is still the whole content of the answer."""
    async with _session(running_server) as session:
        result = await session.call_tool("boom_domain", {})
        assert result.isError is True
        assert "unobtainium" in str(result.content)


def test_upstream_still_chains_a_tool_fault_and_still_does_not_chain_unknown_tool() -> None:
    """The property the sanitiser's exemption reads, asserted against the installed `mcp`.

    `_sanitize_tool_errors` distinguishes a fault from a refusal by `ToolError.__cause__`: upstream
    raises `ToolError(...) from e` when a tool body fails and raises a bare `ToolError` for a name
    it does not have. Neither is documented as a promise, so this pins both — an upstream release
    that starts chaining `Unknown tool` turns the exemption red here rather than letting a fault's
    text reach the model.
    """
    import inspect

    from mcp.server.fastmcp.tools import base, tool_manager

    assert 'raise ToolError(f"Error executing tool {self.name}: {e}") from e' in inspect.getsource(
        base.Tool.run
    )
    source = inspect.getsource(tool_manager.ToolManager.call_tool)
    assert 'raise ToolError(f"Unknown tool: {name}")' in source
    assert "from" not in source.split("Unknown tool")[1].split("\n")[0]


def test_metrics_is_open_and_carries_no_identity(running_server: str) -> None:
    """`/metrics` is unauthenticated on purpose, so what it may carry is the whole control.

    It is *not* "counts only" and never was: `generate_latest(REGISTRY)` on the default registry
    publishes `python_info`, `process_start_time_seconds`, `process_open_fds` and the rest. That is
    fine — an operator wants them, and the NetworkPolicy admits only the agent's pods and the
    monitoring namespace — but the sentence a reviewer relies on when deciding this endpoint may
    stay open has to be the true one, and the true one is: no request content, no caller identity,
    no tool argument.

    Asserted as an absence over the live exposition, because the failure this guards is somebody
    adding `tool_calls_total{tool=..., actor=...}` on the strength of a docstring.
    """
    response = httpx.get(f"{running_server}/metrics", timeout=5.0)
    assert response.status_code == 200
    exposition = response.text.lower()
    for forbidden in ("actor", "session_id", "correlation", "smiles", "authorization", "bearer"):
        assert forbidden not in exposition, (
            f"/metrics is unauthenticated and published {forbidden!r}; a labelled metric on this "
            "endpoint must never carry an actor, a session, a correlation id or a tool argument"
        )
