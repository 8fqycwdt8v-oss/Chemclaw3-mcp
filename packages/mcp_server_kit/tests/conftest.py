"""Running one `connector_app` under a real uvicorn, for the tests that need the transport.

Some kit behaviours are properties of the transport rather than of a function (middleware cost,
body cap on a chunked upload, which thread a tool body runs on, idle session lifetime) and cannot
be seen through an in-process ASGI call.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

# How long a server gets to start before a test gives up on it. Generous, because CI shares a box.
STARTUP_TIMEOUT_SECONDS = 30.0


def free_port() -> int:
    """An ephemeral loopback port, released immediately for uvicorn to claim."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="session")
def serving() -> Callable[..., Any]:
    """A factory: `with serving(app) as base_url:` runs `app` on loopback for the block.

    By default it waits only for the port to answer, since a 503 readiness answer is a successful
    startup. A happy-path test passes `require_ready=True` to wait for the 200.
    """

    @contextmanager
    def _serving(app: Any, *, require_ready: bool = False) -> Iterator[str]:
        port = free_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"{base}/healthz", timeout=1.0)
            except httpx.HTTPError:
                time.sleep(0.05)
                continue
            if not require_ready or response.status_code == 200:
                break
            time.sleep(0.05)
        else:  # pragma: no cover - only reached if the app never starts
            pytest.fail(f"the server did not start within {STARTUP_TIMEOUT_SECONDS:.0f} s")
        try:
            yield base
        finally:
            server.should_exit = True
            thread.join(timeout=10)

    return _serving


@pytest.fixture(scope="session")
def mcp_session() -> Callable[..., Any]:
    """A factory: `async with mcp_session(base, token=...) as session:` — a real MCP handshake.

    A factory so a test can open two sessions or assert from inside one. `headers` carries the
    `X-Chemclaw-*` identity a real Chemclaw3 call sends.
    """

    @asynccontextmanager
    async def _session(
        base: str, *, token: str | None = None, headers: dict[str, str] | None = None
    ) -> AsyncIterator[ClientSession]:
        all_headers = dict(headers or {})
        if token is not None:
            all_headers["Authorization"] = f"Bearer {token}"
        async with (
            httpx.AsyncClient(headers=all_headers) as client,
            streamable_http_client(f"{base}/mcp", http_client=client) as (rx, tx, _),
            ClientSession(rx, tx) as session,
        ):
            await session.initialize()
            yield session

    return _session
