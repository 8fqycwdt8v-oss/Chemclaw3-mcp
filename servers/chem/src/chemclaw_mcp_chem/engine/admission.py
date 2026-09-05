"""How many depictions this server will lay out at once — shed at admission, never queued.

`render_structure` is the one CPU-heavy tool here: it hands `Compute2DCoords` and SVG rasterising
to a worker thread and awaits it. Two independent bounds protect a pod.
`depiction.MAX_DEPICTION_ATOMS` caps a *single* call's cost; this caps how many run *together*.

**RDKit does not release the GIL through a depiction, and this file used to say it did.** The claim
was that "the threads are real parallelism", and that a burst on a pod sized for four therefore
thrashes. Measured on a four-core box, laying out a 241-atom molecule at rising thread counts:

    threads= 1  wall=0.043s  cpu=0.050s  cpu_util=1.15x  throughput=23.1/s
    threads= 2  wall=0.103s  cpu=0.100s  cpu_util=0.97x  throughput=19.5/s
    threads= 4  wall=0.205s  cpu=0.170s  cpu_util=0.83x  throughput=19.5/s
    threads= 8  wall=0.430s  cpu=0.360s  cpu_util=0.84x  throughput=18.6/s
    threads=16  wall=0.977s  cpu=0.780s  cpu_util=0.80x  throughput=16.4/s

Wall clock scales linearly with the thread count and this process never exceeds one core's worth of
CPU at any of them. Whatever this gate admits **runs one at a time**, and Chemclaw3's own
independent measurement of RDKit fingerprinting on four threads (0.91x) says the same thing
about the same library. So the thread pool buys *latency isolation* — a render does not block
the event loop, and `/healthz` keeps answering — and it buys **no throughput at all**.

**What that changes is the remedy, which is the part worth getting right.** Since concurrency here
is serial, a bigger ceiling and a bigger `limits.cpu` add nothing: the only way this server serves
more depictions per second is more pods, which is what `deploy/hpa.yaml` is for. The refusal below
therefore names a replica rather than the knob, because telling a caller to raise a ceiling that
cannot help is worse than telling it nothing.

**And a serialised render is why the ceiling is small rather than why it is large.** RDKit holds the
GIL, so an admitted render delays *everything else in this process* — every other tool call, and the
kubelet's `/healthz` probe, whose `timeoutSeconds` is 3. The worst legal depiction measured 97 ms
(241 atoms; a 249-atom alkane is 22 ms, a 201-atom macrocycle 19 ms, so the polypeptide is the bad
case rather than the atom count). N admitted renders can therefore hold the interpreter for
N x 97 ms, and keeping that under a third of the probe budget leaves the probe two thirds of its
own; `DEFAULT_MAX_CONCURRENT_RENDERS` below is inside that, and
`tests/test_depiction_bound.py` holds the arithmetic rather than a transcription of it.

**This is admission control, and it is deliberately not a clock.** Cancelling the awaiting coroutine
does not stop the worker thread, so a per-call wall clock returns an error to a caller who has gone
while the CPU burn continues. Refusing *before* any work starts is the other thing entirely: nothing
is running, nothing is orphaned, and the caller learns immediately that the pod is full. Refused
rather than queued, for the same reason `servers/calc` refuses — a depiction held at the back of a
queue comes back after `connector.yaml`'s `request_timeout` has expired, computed at the expense of
one somebody is still waiting for. A `ValueError` is the family `connector_app` passes to the caller
verbatim, so the refusal is a number Chemclaw3 can back off on.

This mirrors `servers/calc/engine/admission.py` deliberately: the two servers share no code (one
server never imports another), so the shape is copied, not imported. The bound worth copying is the
design — refuse, do not queue — not the wording: this server's refusal names a replica where
`calc`'s names a knob, because on `calc` a slot really is a core and here it is not.
"""

from __future__ import annotations

import threading

__all__ = [
    "ADMISSION_MARKER",
    "DEFAULT_MAX_CONCURRENT_RENDERS",
    "PROBE_TIMEOUT_SECONDS",
    "WORST_RENDER_SECONDS",
    "Admission",
]

# The attribute `tools._admitted` stamps on a gated tool, and the only thing that tells the coverage
# test which tools are gated. A name rather than a hand-kept list, because the thing that must not
# be forgotten is exactly the thing a forgetful change adds.
ADMISSION_MARKER = "__admission_gated__"

#: The worst *legal* depiction, in seconds, measured here: a 241-atom polypeptide at 97 ms. It is
#: the shape rather than the atom count that costs — a 249-atom alkane is 22 ms and a 201-atom
#: macrocycle 19 ms — so the ceiling is derived from the bad case, not the average one.
WORST_RENDER_SECONDS = 0.1

#: `deploy/deployment.yaml`'s `readinessProbe.timeoutSeconds`. The kubelet's probe is the request
#: that must not be starved by a serialised render, because losing it takes the pod out of service.
PROBE_TIMEOUT_SECONDS = 3

#: How many renders may be in flight, and **the derivation is the probe rather than the core
#: count** — see the module docstring for why the core-count argument this was first written on
#: is false. A render holds the GIL, so N of them hold this interpreter for N x
#: `WORST_RENDER_SECONDS`; keeping that under a third of `PROBE_TIMEOUT_SECONDS` leaves the
#: probe two thirds of its budget and gives
#: a ceiling of 10. Eight is inside it. Overridable with `CHEMCLAW_CHEM_MAX_CONCURRENT_RENDERS`,
#: read in `tools.py`; the number lives here so it sits beside the argument for it and so a test can
#: check the arithmetic without importing a transport.
DEFAULT_MAX_CONCURRENT_RENDERS = 8


class Admission:
    """A count of depictions allowed to run at once, refused rather than queued past it.

    Guarded by a lock rather than an `asyncio.Semaphore`: a slot is taken on the event loop and
    given back from whichever thread or callback finishes the work, and nothing ever waits — a full
    gate is an immediate refusal, so nothing here ever suspends.
    """

    def __init__(self, limit: int) -> None:
        """Args: limit: the most depictions that may run at once. Must be at least one."""
        if limit < 1:
            raise ValueError(f"an admission ceiling of {limit} would refuse every depiction")
        self._limit = limit
        self._lock = threading.Lock()
        self._in_flight = 0

    @property
    def limit(self) -> int:
        """The configured ceiling."""
        return self._limit

    @property
    def in_flight(self) -> int:
        """How many depictions hold a slot right now."""
        with self._lock:
            return self._in_flight

    def acquire(self, what: str) -> None:
        """Take a slot, or refuse in terms the caller can act on.

        Args:
            what: The tool being asked for, named in the refusal.

        Raises:
            ValueError: the ceiling is already reached. Worded for whoever receives it — an agent
                reading a tool error, or Chemclaw3 backing off.
        """
        with self._lock:
            if self._in_flight >= self._limit:
                raise ValueError(
                    f"this server is already rendering {self._limit} structures, which is its "
                    f"configured ceiling, so {what} was refused rather than queued: RDKit holds "
                    "the GIL through a depiction, so admitted renders run one at a time and a "
                    "queued one would come back after the caller had stopped waiting for it. "
                    "Retry once one finishes. Raising CHEMCLAW_CHEM_MAX_CONCURRENT_RENDERS will "
                    "not help — it admits more renders onto the same serialised interpreter, "
                    "making every one of them slower; this server scales by replicas."
                )
            self._in_flight += 1

    def release(self) -> None:
        """Give a slot back. Never below zero, so one double release cannot open the gate."""
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
