"""The offload pool is sized from the cgroup, and a tool body actually lands in it.

Two halves, and the second is the one that would have caught the defect. Sizing arithmetic is easy
to unit-test and easy to leave disconnected — nothing in this fleet asserted where a tool body's
thread came from, which is why `min(32, os.cpu_count() + 4)` governed every server for as long as
it did. So the last test here drives a real tool call over a real socket and asks the tool which
thread it ran on.
"""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP
from mcp_server_kit import executor
from mcp_server_kit.app import connector_app
from mcp_server_kit.executor import cpu_allowance, install_default_executor, thread_pool_size

TOKEN = "test-token-for-the-executor"
TOKEN_ENV = "MCP_KIT_EXECUTOR_TOKEN"


@pytest.fixture(autouse=True)
def no_inherited_pool_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both knobs unset, so a developer's shell cannot make these tests say something else."""
    monkeypatch.delenv("MCP_THREAD_POOL_SIZE", raising=False)
    monkeypatch.delenv("MCP_THREAD_POOL_HEADROOM", raising=False)


def _write_cgroup_v2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contents: str) -> None:
    """Point the v2 reader at a file holding `contents`, and take v1 out of the picture."""
    path = tmp_path / "cpu.max"
    path.write_text(contents)
    monkeypatch.setattr(executor, "_CGROUP_V2_CPU_MAX", path)
    monkeypatch.setattr(executor, "_CGROUP_V1_QUOTA", tmp_path / "absent-quota")
    monkeypatch.setattr(executor, "_CGROUP_V1_PERIOD", tmp_path / "absent-period")


def test_a_cgroup_v2_quota_is_the_cpu_allowance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`cpu: "2"` on a 64-core node is two cores, and this is the file that says so."""
    _write_cgroup_v2(tmp_path, monkeypatch, "200000 100000\n")
    assert cpu_allowance() == pytest.approx(2.0)
    assert thread_pool_size() == 2 + executor.DEFAULT_HEADROOM


def test_a_fractional_cgroup_v2_quota_never_sizes_the_pool_below_one_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 200-millicore pod still needs a thread. Zero workers serves nothing at all."""
    _write_cgroup_v2(tmp_path, monkeypatch, "20000 100000\n")
    assert cpu_allowance() == pytest.approx(1.0)
    assert thread_pool_size() == 1 + executor.DEFAULT_HEADROOM


def test_an_unlimited_cgroup_v2_falls_through_to_the_next_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`max 100000` means "no CPU limit set", not "zero cores" — the misread that sizes a pod
    to one thread on a node with no limits at all.
    """
    _write_cgroup_v2(tmp_path, monkeypatch, "max 100000\n")
    assert cpu_allowance() == pytest.approx(float(len(os.sched_getaffinity(0))))


def test_a_cgroup_v1_quota_is_read_when_v2_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both hierarchies exist in the wild; a v1-only host must not silently fall back to the node.

    This box is one: `/sys/fs/cgroup/cpu.max` does not exist here and
    `/sys/fs/cgroup/cpu/cpu.cfs_quota_us` does.
    """
    quota = tmp_path / "cpu.cfs_quota_us"
    period = tmp_path / "cpu.cfs_period_us"
    quota.write_text("300000\n")
    period.write_text("100000\n")
    monkeypatch.setattr(executor, "_CGROUP_V2_CPU_MAX", tmp_path / "absent")
    monkeypatch.setattr(executor, "_CGROUP_V1_QUOTA", quota)
    monkeypatch.setattr(executor, "_CGROUP_V1_PERIOD", period)
    assert cpu_allowance() == pytest.approx(3.0)


def test_no_cgroup_at_all_degrades_to_the_affinity_mask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A developer laptop, a CI container without the files, a non-Linux kernel: still a number."""
    monkeypatch.setattr(executor, "_CGROUP_V2_CPU_MAX", tmp_path / "absent")
    monkeypatch.setattr(executor, "_CGROUP_V1_QUOTA", tmp_path / "absent")
    monkeypatch.setattr(executor, "_CGROUP_V1_PERIOD", tmp_path / "absent")
    assert cpu_allowance() >= 1.0
    assert thread_pool_size() >= 1


def test_an_explicit_size_wins_and_a_malformed_one_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`MCP_THREAD_POOL_SIZE` is how `servers/chem` says its admission ceiling is wider than a core.

    A typo in it must not take the pool to zero or raise at startup: the server still has to serve.
    """
    _write_cgroup_v2(tmp_path, monkeypatch, "100000 100000\n")
    monkeypatch.setenv("MCP_THREAD_POOL_SIZE", "12")
    assert thread_pool_size() == 12
    monkeypatch.setenv("MCP_THREAD_POOL_SIZE", "twelve")
    assert thread_pool_size() == 1 + executor.DEFAULT_HEADROOM
    monkeypatch.delenv("MCP_THREAD_POOL_SIZE")
    monkeypatch.setenv("MCP_THREAD_POOL_HEADROOM", "0")
    assert thread_pool_size() == 1


async def test_to_thread_lands_in_the_installed_pool() -> None:
    """`install_default_executor` makes it the loop's default; `asyncio.to_thread` follows."""
    pool = install_default_executor(server="probe")
    try:
        name = await asyncio.to_thread(lambda: threading.current_thread().name)
        assert name.startswith("probe-tool"), name
        assert pool._max_workers == thread_pool_size()
    finally:
        pool.shutdown(wait=False)


def _free_port() -> int:
    """An ephemeral loopback port, released immediately for uvicorn to claim."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def _serving(app: object) -> Iterator[str]:
    """Run one app under uvicorn on a free loopback port for one test's duration."""
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{base}/healthz", timeout=1.0)
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:  # pragma: no cover - only reached if the app never accepts a connection
        pytest.fail("the server did not accept a connection within 30 s")
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@asynccontextmanager
async def _session(base: str) -> AsyncIterator[ClientSession]:
    """An initialised MCP session against the running server, carrying the bearer token."""
    async with (
        httpx.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}) as client,
        streamable_http_client(f"{base}/mcp", http_client=client) as (rx, tx, _),
        ClientSession(rx, tx) as session,
    ):
        await session.initialize()
        yield session


async def test_a_served_tool_body_offloads_into_the_sized_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The end-to-end half, and the one nothing in this fleet asserted before.

    A tool that offloads with `asyncio.to_thread` — which is every heavy tool here — reports the
    thread it ran on. Without `install_default_executor` that thread is named `asyncio_N` and comes
    from a pool CPython sized from the *node*; with it, the name says which server owns the pool
    and the width is the one this pod's cgroup justifies. The size is pinned through the env knob
    so the assertion is about plumbing rather than about this box's core count.
    """
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    monkeypatch.setenv("MCP_THREAD_POOL_SIZE", "3")
    server = FastMCP("pooled")

    @server.tool()
    async def which_thread() -> str:
        """Report the worker thread this tool body ran on."""
        return await asyncio.to_thread(lambda: threading.current_thread().name)

    app = connector_app(server, name="pooled", token_env=TOKEN_ENV)
    with _serving(app) as base:
        async with _session(base) as session:
            result = await session.call_tool("which_thread", {})
    reported = result.content[0].text  # type: ignore[union-attr]
    assert reported.startswith("pooled-tool"), reported
    # `_N` is the worker index within the pool, so this also says the pool was capped at three.
    assert int(reported.rsplit("_", 1)[1]) < 3, reported
