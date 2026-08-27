"""How many calculations this server will run at once — shed at admission, never queued.

Every heavy tool here hands its work to `asyncio.to_thread` and awaits it, and nothing counted how
many of those were in flight. So the aggregate load on a pod was whatever its callers happened to
send: the image pins `OMP_NUM_THREADS=1` so one calculation is one core, and a burst of twenty
therefore thrashes a pod sized for four, every one of them slower than it would have been alone.
That is the shape that feeds itself — each call takes longer, the caller's own timeout fires, and
`cached_compute` is check-then-act, so the retry starts another identical burn beside the ones
already running.

**This is admission control, and it is deliberately not a clock.** `CLAUDE.md` argues at length that
a per-call wall clock in the transport is a control that reads as one and is not: cancelling the
awaiting coroutine does not stop the worker thread, so such a timeout returns an error to a caller
who has already gone while the CPU burn continues. That argument is about *abandoning work already
started*. Refusing before any work starts is the other thing entirely — nothing is running, nothing
is orphaned, and the caller learns immediately that this pod is full rather than after minutes of
waiting behind a queue. The two controls compose: this bounds how much starts, `budget.Deadline`
bounds how long one may run, and `xtb_cli.run_isolated` kills the process group of the one case that
escapes both.

**Refusing rather than queueing is the whole design, and it is not the obvious choice.** A queue
looks kinder and is not: the calculations here are seconds to hours, so a call held at the back of
one comes back long after `connector.yaml`'s `request_timeout` has expired — an answer nobody is
waiting for, computed at the expense of the ones somebody is. A prompt refusal is a number the
caller can act on: Chemclaw3 knows its own concurrency and can back off, and a `ValueError` is the
family `connector_app` passes to the caller verbatim.

**The three tools that run no SCF are deliberately outside this.** `calculation_key`,
`embed_structure` and `combine_structures` are milliseconds of RDKit, and they are exactly what a
caller needs answerable *while* the pod is full: `calculation_key` is how a client asks "have I
computed this already?" before paying for it, so refusing it under load would push work onto a pod
that is already saturated. That split is not a judgement re-made per tool — it is the manifest's
own `read_only`/`state_changing` classification, and `tests/test_admission.py` checks the served
surface against it.

**One honest limit.** The slot is held for as long as the awaiting coroutine lives, so a client that
disconnects mid-calculation releases it while its worker thread is still running. The alternative —
holding the slot until the thread finishes — is what this does, by keeping the inner task alive
through `asyncio.shield` and releasing on *its* completion rather than on the awaiting caller's; see
`tools._admitted`. What that cannot cover is the process dying, which needs no accounting.
"""

from __future__ import annotations

import threading

__all__ = ["ADMISSION_MARKER", "Admission"]

# The attribute `tools._admitted` stamps on a gated tool, and the only thing that tells the coverage
# test which tools are gated. A name rather than a hand-kept list, because the thing that must not
# be forgotten is exactly the thing a forgetful change adds.
ADMISSION_MARKER = "__admission_gated__"


class Admission:
    """A count of calculations allowed to run at once, refused rather than queued past it.

    Guarded by a lock rather than an `asyncio.Semaphore`: a slot is taken on the event loop and
    given back from whichever thread or callback finishes the work, and nothing ever waits —
    a full gate is an immediate refusal, so nothing here ever suspends.
    """

    def __init__(self, limit: int) -> None:
        """Args: limit: the most calculations that may run at once. Must be at least one."""
        if limit < 1:
            raise ValueError(f"an admission ceiling of {limit} would refuse every calculation")
        self._limit = limit
        self._lock = threading.Lock()
        self._in_flight = 0

    @property
    def limit(self) -> int:
        """The configured ceiling."""
        return self._limit

    @property
    def in_flight(self) -> int:
        """How many calculations hold a slot right now."""
        with self._lock:
            return self._in_flight

    def acquire(self, what: str) -> None:
        """Take a slot, or refuse in terms the caller can act on.

        Args:
            what: The calculation being asked for, named in the refusal — the caller's levers are
                which tool it called and when, so the message has to say which one was turned away.

        Raises:
            ValueError: the ceiling is already reached. Worded for whoever receives it, which is
                Chemclaw3's `cached_compute` or an agent reading a tool error.
        """
        with self._lock:
            if self._in_flight >= self._limit:
                raise ValueError(
                    f"this server is already running {self._limit} calculations, which is its "
                    f"configured ceiling, so {what} was refused rather than queued: every "
                    "calculation here is seconds to hours of CPU with one core each, and a queued "
                    "one would come back after the caller had stopped waiting for it. Retry once "
                    "one finishes, or raise CHEMCLAW_CALC_MAX_CONCURRENT_REQUESTS on a pod sized "
                    "for more"
                )
            self._in_flight += 1

    def release(self) -> None:
        """Give a slot back. Never below zero, so one double release cannot open the gate."""
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
