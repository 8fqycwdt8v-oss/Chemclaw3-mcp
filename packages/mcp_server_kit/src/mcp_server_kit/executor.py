"""The pool every `asyncio.to_thread` in a server shares — sized from the cgroup, not the node.

Every heavy tool in this fleet offloads with `asyncio.to_thread`, which is
`loop.run_in_executor(None, ...)`: the loop's *single* default `ThreadPoolExecutor`. Nobody creates
that pool, so CPython does, at `min(32, os.cpu_count() + 4)` — and `os.cpu_count()` reports the
**node's** processors, not the container's allowance. On an OpenShift worker with 64 cores, a pod
with `cpu: "1"` therefore gets a 32-thread offload budget for one core's worth of CPU. Measured on
this 4-core box, `pyexec` peaked at exactly 8 concurrent child processes (`min(32, 4 + 4)`) and
degraded linearly past it; the same image on a 64-core node would admit 32.

Oversubscription of this pool is not the harmless queueing it looks like, because what the threads
hold is not just CPU:

- **It buys no throughput.** The work is GIL-bound. Driving `chem` at rising concurrency, its own
  CPU utilisation stayed between 0.91x and 0.98x of one core from 1 to 16 concurrent calls and
  throughput was flat at ~22/s — so 32 threads and 5 threads compute the same amount, and the only
  difference is how many callers are told "later" versus how many wait longer for the same answer.
- **It defeats a per-call CPU bound.** Any tool whose limit is stated in *CPU* seconds against a
  *wall* clock is broken by oversubscription: N of them on one core need N times the wall clock to
  spend the same budget, so each is killed on the wall limit and the caller gets a timeout instead
  of a worded refusal. `servers/pyexec` is the worked example — 15 CPU-seconds against a 20 s wall
  clock — and the reason its measured concurrency was 8 on this box was this pool and nothing else.
  It has an admission ceiling of its own now, which is the right place for the bound; this pool is
  the floor under every server that has not written one.

So the pool is sized here, once, from what the container is actually allowed to spend. The formula
is CPython's own — the CPU allowance plus a small headroom — with the *truth* substituted for
`os.cpu_count()`. The headroom exists because not every offload is chemistry: it is what keeps a
short call off the back of the queue when the CPU-bound slots are full.

**A server whose admission ceiling exceeds this pool has an admission ceiling that is not the real
limit**, and it says so with `MCP_THREAD_POOL_SIZE` in its deployment rather than by editing code —
`servers/chem` admits 8 concurrent depictions on `cpu: "1"`, which this would give 5 threads. That
costs it nothing measurable (see the GIL figures above) but it is the server's decision to state,
not this module's to guess: the kit cannot see a server's `engine/admission.py`.

Only `asyncio.to_thread` and bare `run_in_executor(None, ...)` land here. `anyio.to_thread.run_sync`
— which Starlette uses for a synchronous route handler — has a limiter of its own and is untouched;
nothing in this fleet's tool path goes through it.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["cpu_allowance", "install_default_executor", "thread_pool_size"]

# cgroup v2: one file holding "<quota> <period>", or "max <period>" when the pod has no CPU limit.
_CGROUP_V2_CPU_MAX = Path("/sys/fs/cgroup/cpu.max")
# cgroup v1: the same pair, in two files, with a quota of -1 meaning unlimited.
_CGROUP_V1_QUOTA = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
_CGROUP_V1_PERIOD = Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us")

# Threads reserved for work that is not a tool body: a readiness probe's own pool is separate, but
# a bearer check, a metrics scrape and an SSE reconnect all want a thread that is not queued behind
# a geometry optimisation. Four is CPython's own headroom over the CPU count, kept deliberately.
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

    In order of authority: the cgroup v2 quota, the cgroup v1 quota, the CPU affinity mask, and
    finally `os.cpu_count()`. A cgroup quota is the only one of the four that reflects a Kubernetes
    `limits.cpu`, and it is read from whichever hierarchy the container is on — both exist in the
    wild, and a v1-only host is not an exotic case. The affinity mask is next because
    `cpuset`-pinned pods report it truthfully where a quota is absent, and `os.cpu_count()` is the
    last resort precisely because it is the number that is wrong on every limited pod.

    Never returns less than one core: a pod may be limited to 100 millicores, and a thread pool of
    zero serves nothing at all.
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

    `MCP_THREAD_POOL_SIZE` states it outright — the knob a server whose admission ceiling is wider
    than its CPU allowance sets in its deployment. Otherwise it is the CPU allowance rounded up,
    plus `MCP_THREAD_POOL_HEADROOM`, which is CPython's own arithmetic over a number that is true.
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
    return math.ceil(cpu_allowance()) + reserve


def install_default_executor(*, server: str) -> ThreadPoolExecutor:
    """Make a right-sized pool the running loop's default, and say how wide it is.

    Called from `connector_app`'s lifespan — the process's "about to serve" moment, and the first
    one where there is a running loop to install it on. From then on every `asyncio.to_thread` in
    every tool body lands in it with no plumbing at any call site.

    Args:
        server: The server's name, for the one log line an operator reads to find out how much
            offload this pod actually has.

    Returns:
        The installed executor, so the lifespan can shut it down and a test can assert its width.
    """
    width = thread_pool_size()
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
