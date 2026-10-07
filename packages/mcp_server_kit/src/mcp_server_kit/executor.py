"""The pool every `asyncio.to_thread` in a server shares — sized from the cgroup, not the node.

CPython sizes the default executor as `min(32, os.cpu_count() + 4)`, and `os.cpu_count()` reports
the node, so a one-core pod on a large node gets 32 threads. Oversubscription buys no throughput
(the work is GIL-bound) and breaks any per-call CPU budget enforced against a wall clock. This uses
CPython's formula with the container's real CPU allowance.

A server whose admission ceiling is wider than this pool sets `MCP_THREAD_POOL_SIZE` in its
deployment. Only `asyncio.to_thread` and `run_in_executor(None, ...)` land here; anyio's thread
pool is separate.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mcp_server_kit.limits import report_bound

logger = logging.getLogger(__name__)

__all__ = ["cpu_allowance", "install_default_executor", "thread_pool_size"]

# cgroup v2: one file holding "<quota> <period>", or "max <period>" when the pod has no CPU limit.
_CGROUP_V2_CPU_MAX = Path("/sys/fs/cgroup/cpu.max")
# cgroup v1: the same pair, in two files, with a quota of -1 meaning unlimited.
_CGROUP_V1_QUOTA = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
_CGROUP_V1_PERIOD = Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us")

# Threads for work that is not a tool body (bearer checks, scrapes, SSE reconnects), so it does not
# queue behind a calculation. CPython's own headroom.
DEFAULT_HEADROOM = 4


def _quota_from_cgroup_v2() -> float | None:
    """Cores this cgroup may spend under cgroup v2, or `None` if v2 is absent or sets no limit."""
    try:
        quota, period = _CGROUP_V2_CPU_MAX.read_text().split()
    except (OSError, ValueError):
        return None
    if quota == "max":
        return None
    try:
        return int(quota) / int(period)
    except (ValueError, ZeroDivisionError):  # pragma: no cover - a kernel writing nonsense
        return None


def _quota_from_cgroup_v1() -> float | None:
    """Cores this cgroup may spend under cgroup v1, or `None` if v1 is absent or sets no limit."""
    try:
        quota = int(_CGROUP_V1_QUOTA.read_text().strip())
        period = int(_CGROUP_V1_PERIOD.read_text().strip())
    except (OSError, ValueError):
        return None
    if quota <= 0 or period <= 0:
        return None
    return quota / period


def cpu_allowance() -> float:
    """How many cores this process may actually spend, as a fraction if the quota is fractional.

    In order of authority: cgroup v2 quota, cgroup v1 quota, CPU affinity mask, `os.cpu_count()`.
    Only a cgroup quota reflects a Kubernetes `limits.cpu`. Never less than one core.
    """
    for reader in (_quota_from_cgroup_v2, _quota_from_cgroup_v1):
        quota = reader()
        if quota is not None:
            return max(quota, 1.0)
    try:
        return float(len(os.sched_getaffinity(0)))
    except AttributeError:  # pragma: no cover - not Linux
        return float(os.cpu_count() or 1)


def thread_pool_size() -> int:
    """The width this process's default executor should have.

    `MCP_THREAD_POOL_SIZE` if set, else the CPU allowance rounded up plus
    `MCP_THREAD_POOL_HEADROOM`.
    """
    explicit = os.environ.get("MCP_THREAD_POOL_SIZE", "").strip()
    if explicit:
        try:
            return max(int(explicit), 1)
        except ValueError:
            logger.warning(
                "MCP_THREAD_POOL_SIZE=%r is not an integer; sizing from the cgroup", explicit
            )
    headroom = os.environ.get("MCP_THREAD_POOL_HEADROOM", "").strip()
    try:
        reserve = max(int(headroom), 0) if headroom else DEFAULT_HEADROOM
    except ValueError:
        logger.warning(
            "MCP_THREAD_POOL_HEADROOM=%r is not an integer; using %d", headroom, DEFAULT_HEADROOM
        )
        reserve = DEFAULT_HEADROOM
    report_bound("MCP_THREAD_POOL_HEADROOM", reserve)
    return math.ceil(cpu_allowance()) + reserve


def install_default_executor(*, server: str) -> ThreadPoolExecutor:
    """Make a right-sized pool the running loop's default, and say how wide it is.

    Called from `connector_app`'s lifespan, the first moment with a running loop.

    Args:
        server: The server's name, for the log line stating the pool width.

    Returns:
        The installed executor, so the lifespan can shut it down.
    """
    width = thread_pool_size()
    # The width actually installed, under the knob that sets it outright — so a pod sized from its
    # cgroup and one an operator sized by hand both answer the same question on `/healthz`.
    report_bound("MCP_THREAD_POOL_SIZE", width)
    executor = ThreadPoolExecutor(max_workers=width, thread_name_prefix=f"{server}-tool")
    asyncio.get_running_loop().set_default_executor(executor)
    logger.info(
        "server %s: to_thread pool sized %d (cpu allowance %.2f, cpython default would be %d)",
        server,
        width,
        cpu_allowance(),
        min(32, (os.cpu_count() or 1) + 4),
    )
    return executor
