"""Time one `calc` `calculation_key` call over MCP and over `/rpc`, on loopback.

`calculation_key` is the call Chemclaw3 makes before every cache lookup, so its round trip is the
floor of a cache hit. Five ways of making it are timed against the real `calc` app, one call at a
time, after a warm-up:

- `mcp_session_per_call` — a fresh MCP session (`initialize`, the call, teardown): what a caller
  without a session pool pays;
- `mcp_warm_session` — the call alone on an open session;
- `rpc_pooled` — `POST /rpc/calculation_key` on one kept-alive `httpx.AsyncClient`;
- `rpc_new_connection` — the same POST with keep-alive off, so each call opens a TCP connection;
- `livez_pooled` — `GET /livez` on the pooled client, the HTTP stack with no tool behind it.

`--server thread` runs the app under uvicorn in a thread of this process (the model the
architecture baseline and every `test_server.py` use); `--server process` runs it as its own
`uvicorn` process, so client and server do not share a GIL. Prints one JSON document; `--out`
also writes it. Loopback only: a cluster hop adds the network's round trip to every row alike.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
from chemclaw_contracts.calc import CalculationKeyRequest
from chemclaw_contracts.rpc import rpc_path
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

TOKEN = "bench-rpc-vs-mcp-token"  # noqa: S105 - a throwaway loopback credential
TOKEN_ENV = "CHEMCLAW_CALC_TOKEN"  # noqa: S105 - an environment variable's *name*
APP = "chemclaw_mcp_calc.app:app"
STARTUP_TIMEOUT_S = 120.0
WARMUP_CALLS = 20
DEFAULT_CALLS = 300
DEFAULT_SESSION_CALLS = 60
REQUEST = CalculationKeyRequest(tool="predict_solubility", arguments={"smiles": "CCO"})


def _free_port() -> int:
    """An ephemeral loopback port, released for the server to claim."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_ready(base: str) -> None:
    """Block until `/healthz` answers 200, or fail naming how long was allowed."""
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/healthz", timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.1)
    raise RuntimeError(f"calc did not become ready within {STARTUP_TIMEOUT_S:.0f} s")


@contextmanager
def _serving(mode: str) -> Iterator[str]:
    """The `calc` app on loopback for the block, in a thread of this process or as a child."""
    os.environ[TOKEN_ENV] = TOKEN
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    if mode == "thread":
        import uvicorn
        from chemclaw_mcp_calc.app import app

        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            _wait_ready(base)
            yield base
        finally:
            server.should_exit = True
            thread.join(timeout=10)
        return
    argv = [sys.executable, "-m", "uvicorn", APP, "--host", "127.0.0.1", "--port", str(port)]
    child = subprocess.Popen([*argv, "--log-level", "error"], env=os.environ.copy())
    try:
        _wait_ready(base)
        yield base
    finally:
        child.terminate()
        child.wait(timeout=15)


def _summary(seconds: list[float]) -> dict[str, float]:
    """Milliseconds: the median, the 95th percentile, the mean and the fastest."""
    ordered = sorted(seconds)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    return {
        "calls": float(len(ordered)),
        "p50_ms": round(statistics.median(ordered) * 1000, 2),
        "p95_ms": round(p95 * 1000, 2),
        "mean_ms": round(statistics.fmean(ordered) * 1000, 2),
        "min_ms": round(ordered[0] * 1000, 2),
    }


async def _timed(call: Callable[[], Awaitable[None]], calls: int) -> list[float]:
    """Warm up, then time `calls` sequential awaits of `call`."""
    for _ in range(WARMUP_CALLS):
        await call()
    out: list[float] = []
    for _ in range(calls):
        started = time.perf_counter()
        await call()
        out.append(time.perf_counter() - started)
    return out


async def _mcp_call(session: ClientSession) -> None:
    """One `calculation_key` over an open MCP session."""
    result = await session.call_tool(REQUEST.tool_name, REQUEST.wire())
    if result.isError:
        raise RuntimeError(f"calculation_key over MCP failed: {result.content}")


async def _rpc_call(client: httpx.AsyncClient, base: str) -> None:
    """One `calculation_key` over `/rpc` on `client`."""
    response = await client.post(f"{base}{rpc_path(REQUEST.tool_name)}", json=REQUEST.wire())
    if response.status_code != 200:
        raise RuntimeError(f"calculation_key over /rpc failed: {response.status_code}")


async def measure(base: str, calls: int, session_calls: int) -> dict[str, dict[str, float]]:
    """Time the five ways against the server at `base`."""
    headers = {"Authorization": f"Bearer {TOKEN}"}
    out: dict[str, dict[str, float]] = {}

    async def per_call_session() -> None:
        async with (
            httpx.AsyncClient(headers=headers, timeout=30.0) as http,
            streamable_http_client(f"{base}/mcp", http_client=http) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            await _mcp_call(session)

    out["mcp_session_per_call"] = _summary(await _timed(per_call_session, session_calls))

    async with (
        httpx.AsyncClient(headers=headers, timeout=30.0) as http,
        streamable_http_client(f"{base}/mcp", http_client=http) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        out["mcp_warm_session"] = _summary(await _timed(lambda: _mcp_call(session), calls))

    async with httpx.AsyncClient(headers=headers, timeout=30.0) as pooled:
        out["rpc_pooled"] = _summary(await _timed(lambda: _rpc_call(pooled, base), calls))

        async def probe() -> None:
            await pooled.get(f"{base}/livez")

        out["livez_pooled"] = _summary(await _timed(probe, calls))

    # One client with keep-alive off: every request opens a connection, and the client's own
    # construction (its TLS context) is not in the timing.
    unpooled = httpx.Limits(max_keepalive_connections=0)
    async with httpx.AsyncClient(headers=headers, timeout=30.0, limits=unpooled) as fresh:
        out["rpc_new_connection"] = _summary(await _timed(lambda: _rpc_call(fresh, base), calls))
    return out


def main() -> None:
    """Measure in each requested server mode and print the numbers as JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", choices=("thread", "process", "both"), default="both")
    parser.add_argument("--calls", type=int, default=DEFAULT_CALLS)
    parser.add_argument("--session-calls", type=int, default=DEFAULT_SESSION_CALLS)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    modes = ("thread", "process") if args.server == "both" else (args.server,)
    report: dict[str, Any] = {"request": REQUEST.wire(), "python": sys.version.split()[0]}
    for mode in modes:
        with _serving(mode) as base:
            report[f"server_in_{mode}"] = asyncio.run(measure(base, args.calls, args.session_calls))
    text = json.dumps(report, indent=2)
    print(text)
    if args.out is not None:
        args.out.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
