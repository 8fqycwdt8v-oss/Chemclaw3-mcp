"""How many programs this server will run at once — shed at admission, never queued.

Every per-*call* bound here was already thorough: 20 s of wall clock enforced with `killpg`, 15 CPU
seconds, 16 MB of file writes, 128 descriptors, and no fork headroom at all. What none of them
bounded is how many calls run **together**, and without that the effective ceiling was an accident
of CPython's defaults: `asyncio.to_thread` uses the default executor, which is
`min(32, os.cpu_count() + 4)` threads — and `os.cpu_count()` is **not cgroup-aware**, so on a
64-core OpenShift node a pod limited to two cores would still admit 32 concurrent child processes.

**That accident is worse than a wrong number, because it breaks the two bounds that are right.** A
run gets 15 CPU seconds and 20 s of wall clock, and those only compose while a run has roughly a
core: 32 single-threaded children on 2 cores means a program using its budget needs 240 s of wall
clock against a 20 s limit, so *every* run is SIGKILLed as a timeout. The chemist is then told the
analysis timed out — a statement about their program — when what happened is that the pod was full.
Measured on a four-core box before this gate existed: concurrency 1 answered in 0.54 s, concurrency
16 in 1.94 s p50, concurrency 40 in 3.92 s, with peak child processes pinned at 8 by that same
default executor.

So the ceiling is **cores**, and it is stated rather than inherited: a run is one child pinned to
one thread (`sandbox._environment` sets `OMP_NUM_THREADS=1` and its siblings), so a slot is a core,
and `deploy/deployment.yaml` sets `limits.cpu` to the same number. It is also what
`limits.default_memory_bytes` divides the pod's memory limit by, so the two resources are bounded by
one number instead of two.

**Refused rather than queued**, which is the same argument `servers/calc` and `servers/chem` make
and it is sharper here: a program held at the back of a queue burns its 20 s wall clock waiting and
is then killed for it, so queueing does not delay a run, it destroys one. A prompt `ValueError` is
the family `connector_app` passes to the caller verbatim, so the model is told the pod is full
rather than being told its program was too slow.

The shape is copied from `servers/chem/engine/admission.py` rather than imported — one server never
imports another, and the wording has to name this server's own tool and knob.
"""

from __future__ import annotations

import threading

__all__ = ["ADMISSION_MARKER", "Admission"]

# The attribute `tools._admitted` stamps on a gated tool, and the only thing that tells the coverage
# test which tools are gated. A name rather than a hand-kept list, because the thing that must not
# be forgotten is exactly the thing a forgetful change adds.
ADMISSION_MARKER = "__admission_gated__"


class Admission:
    """A count of sandboxed runs allowed at once, refused rather than queued past it.

    Guarded by a lock rather than an `asyncio.Semaphore`: a slot is taken on the event loop and
    given back from whichever thread or callback finishes the work, and nothing ever waits — a full
    gate is an immediate refusal, so nothing here ever suspends.
    """

    def __init__(self, limit: int) -> None:
        """Args: limit: the most runs that may execute at once. Must be at least one."""
        if limit < 1:
            raise ValueError(f"an admission ceiling of {limit} would refuse every run")
        self._limit = limit
        self._lock = threading.Lock()
        self._in_flight = 0

    @property
    def limit(self) -> int:
        """The configured ceiling."""
        return self._limit

    @property
    def in_flight(self) -> int:
        """How many runs hold a slot right now."""
        with self._lock:
            return self._in_flight

    def acquire(self, what: str) -> None:
        """Take a slot, or refuse in terms the caller can act on.

        Args:
            what: The tool being asked for, named in the refusal.

        Raises:
            ValueError: the ceiling is already reached. Worded for whoever receives it — an agent
                reading a tool error, which can retry or send a smaller analysis.
        """
        with self._lock:
            if self._in_flight >= self._limit:
                raise ValueError(
                    f"this server is already running {self._limit} analyses, which is its "
                    f"configured ceiling, so {what} was refused rather than queued: each run is a "
                    "whole core for up to its wall-clock limit, and a queued one would spend that "
                    "limit waiting and then be killed for exceeding it. Retry once one finishes, "
                    "or raise CHEMCLAW_PYEXEC_MAX_CONCURRENT_RUNS on a pod with more cores and "
                    "more memory — the per-run address-space bound is the pod's memory divided by "
                    "this number."
                )
            self._in_flight += 1

    def release(self) -> None:
        """Give a slot back. Never below zero, so one double release cannot open the gate."""
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
