"""How many depictions this server accepts at once — a caller past the ceiling is refused, not held.

`render_structure` is the one CPU-heavy tool here: it hands `Compute2DCoords` and SVG rasterising
to a worker thread and awaits it. Two bounds in this server protect a pod, and a third that it does
not own — the thread pool the offload lands in — is argued about further down.
`depiction.MAX_DEPICTION_ATOMS` caps a *single* call's cost; this caps how many are **in flight** —
accepted and not yet answered. It is deliberately not called a cap on how many run *together*,
which is what this file used to say and what no configuration of this pod can make true: the third
bound below is measured, and on one core eight admitted renders never run together at any thread
pool width.

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

**There is a third bound, it disagrees with this one, and measuring it is what decided that
neither number moves.** Every tool body here offloads with `asyncio.to_thread`, into the process's
*default* executor — a pool `mcp_server_kit.executor` sizes from the container's cgroup rather than
from the node, at `ceil(limits.cpu) + 4`. On this server's `limits.cpu: "1"` that is **5 threads**
against a ceiling of **8**, and the kit's own docstring invites a server in exactly this position to
say so with `MCP_THREAD_POOL_SIZE` in its deployment. Driven end to end against a running server —
eight concurrent `render_structure` calls of the worst legal molecule, one MCP session each, with a
kubelet-shaped `/healthz` poll running beside them — the disagreement turns out to have no
consequence a caller can observe, so the knob is **left unset**:

    pool= 5  p50 541 ms  p95 748 ms  refused 0/48  /healthz 127/127 ok, p50 4 ms, max 450 ms
    pool= 8  p50 532 ms  p95 540 ms  refused 0/24  /healthz  59/59  ok, p50 3 ms, max 364 ms
    pool=12  p50 601 ms  p95 785 ms  refused 0/48  /healthz 126/126 ok, p50 4 ms, max 408 ms

Interleaved runs put the three inside each other's noise (p50 586/607/530 ms against 588/598/592 ms
over four alternating passes), and the same holds for a *mixed* burst — eight renders plus ten
`resolve_compound` calls arriving 100 ms in, the case the kit's four-thread headroom exists for —
whose cheap-call median was 268 to 438 ms at every width with no ordering across repeats.

**The reason is structural rather than lucky, and it is what makes "raise the pool" the wrong
remedy.** This pod may spend one core, so a second runnable thread cannot make a render finish
sooner whatever the GIL does; and the GIL means the pool never even *grows* to its ceiling — eight
concurrent renders through a 12-wide pool spawn 4 to 6 worker threads, exactly as a 5-wide pool
does, because a worker that finishes takes the next queued item before `ThreadPoolExecutor` decides
it needs another thread. A wider pool would therefore be a bound that is never reached: a knob that
renders nothing, which is the defect this fleet keeps finding rather than a fix for one. Lowering
*this* ceiling to 5 to match is the other tempting move and is worse than nothing — it would refuse
three of eight concurrent callers that the pod answers today in 0.6 s against a 30 s
`request_timeout`, and buy no throughput, because throughput here is one core either way. Raising
`limits.cpu` buys nothing for the same reason and would additionally break `deploy/hpa.yaml`, whose
utilisation target is calibrated against a request set to a saturated pod's real draw.

**What the narrow pool does change is a promise, and that is the part that needed fixing.** With
five threads, renders 6, 7 and 8 are admitted and then wait for a *worker* rather than for the CPU —
queued, which is the thing this gate says it never does. It is harmless because the wait is inside
the same product the ceiling was already derived from (nothing else can run while a render holds the
interpreter, so an admitted caller waits at most `DEFAULT_MAX_CONCURRENT_RENDERS x
WORST_RENDER_SECONDS` whether it waits on a thread or on the GIL), and the readiness probe is
insulated from it outright — `connector_app` gives `readiness` a private one-thread pool, so
`/healthz` never queues behind a depiction at all. But it is queueing, so the ceiling below is a
bound on renders **in flight** rather than on renders **running**, and `POD_THREAD_POOL_WIDTH`
records the other number so the two cannot drift apart unwatched.

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
    "POD_THREAD_POOL_WIDTH",
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

#: The width of the process's default `asyncio.to_thread` pool on the shipped pod: what
#: `mcp_server_kit.executor.thread_pool_size()` returns for `deploy/deployment.yaml`'s
#: `limits.cpu: "1"`, which is `ceil(1)` plus the kit's four-thread headroom. It is **narrower than
#: the ceiling above**, deliberately and measurably harmlessly — the module docstring has the
#: measurement and the three remedies it rejects. It is written down rather than left implicit
#: because it is an input to that argument, and `tests/test_depiction_bound.py` re-derives it from
#: the Deployment through the kit's own arithmetic: `limits.cpu`, an `MCP_THREAD_POOL_SIZE` added to
#: the pod, or a change to the kit's headroom cannot move without this number and the paragraph
#: beside it being revisited.
POD_THREAD_POOL_WIDTH = 5


class Admission:
    """A count of depictions allowed in flight at once, refused rather than queued past it.

    Guarded by a lock rather than an `asyncio.Semaphore`: a slot is taken on the event loop and
    given back from whichever thread or callback finishes the work, and nothing ever waits — a full
    gate is an immediate refusal, so nothing here ever suspends.
    """

    def __init__(self, limit: int) -> None:
        """Args: limit: the most depictions that may be in flight at once. At least one."""
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
