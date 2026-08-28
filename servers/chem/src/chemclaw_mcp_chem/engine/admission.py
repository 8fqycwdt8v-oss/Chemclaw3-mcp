"""How many depictions this server will lay out at once — shed at admission, never queued.

`render_structure` is the one CPU-heavy tool here: it hands `Compute2DCoords` and SVG rasterising
to a worker thread and awaits it. Two independent bounds protect a pod.
`depiction.MAX_DEPICTION_ATOMS` caps a *single* call's cost; this caps how many run *together*,
because the atom bound still lets a
burst of large-but-legal depictions saturate a pod's cores. Every render is one core (RDKit releases
the GIL for the heavy passes, so the threads are real parallelism), so a burst of them on a pod
sized for four thrashes — each slower than it would have been alone, the caller's own timeout fires,
and a retry starts another render beside the ones already running.

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
design — refuse, do not queue — not the wording, which names this server's own tool and knob.
"""

from __future__ import annotations

import threading

__all__ = ["ADMISSION_MARKER", "Admission"]

# The attribute `tools._admitted` stamps on a gated tool, and the only thing that tells the coverage
# test which tools are gated. A name rather than a hand-kept list, because the thing that must not
# be forgotten is exactly the thing a forgetful change adds.
ADMISSION_MARKER = "__admission_gated__"


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
                    f"configured ceiling, so {what} was refused rather than queued: laying out a "
                    "large molecule is CPU-bound with one core each, and a queued one would come "
                    "back after the caller had stopped waiting for it. Retry once one finishes, or "
                    "raise CHEMCLAW_CHEM_MAX_CONCURRENT_RENDERS on a pod sized for more."
                )
            self._in_flight += 1

    def release(self) -> None:
        """Give a slot back. Never below zero, so one double release cannot open the gate."""
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
