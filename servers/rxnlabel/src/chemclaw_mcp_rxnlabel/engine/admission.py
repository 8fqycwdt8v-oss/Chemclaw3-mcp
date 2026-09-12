"""How much of this pod a batch may occupy at once — shed at admission, never queued.

`MAX_BATCH` bounds how much *one* request asks for; nothing bounded how many such requests are in
flight. The two are different bounds and the fleet's rule says so explicitly (`CLAUDE.md`, *"that
last one is not the same bound as the first"*), and the caller here is a corpus drain rather than a
chemist: Chemclaw3's background labeller walks a multi-million-row corpus in batches, so "many
maximal batches arriving together" is this server's *normal* traffic pattern, not its pathological
one.

**What a batch costs, measured.** Driving `_represent` over a real reaction with five species, in a
checkout with the optional mapper absent so only the RDKit path runs: 2.8 ms per reaction, flat
from a batch of 10 to a batch of 500 (0.028 s, 0.275 s, 1.403 s), and the wall clock equals the CPU
time to three digits. Raising the thread count does not raise throughput — 1, 2 and 4 threads
labelling 50 reactions each gave 581, 425 and 418 reactions/s at a CPU utilisation of 0.87x, 1.04x
and 1.14x of a single core. **RDKit holds the GIL through SMARTS matching**, which is the same
finding `servers/chem` measured for depiction and Chemclaw3 measured for fingerprinting, so on this
path admitted batches run one at a time whatever the ceiling says.

**A slot is a core, not a call, and the mapper is the whole reason that distinction is here.**
RXNMapper is an ALBERT transformer, and torch releases the GIL and parallelises *inside* one call:
its intra-op width is `torch.get_num_threads()`, which torch sizes from the machine's physical
cores and **not** from the container's cgroup — so a pod limited to two cores on a 64-core node
gives one forward pass 64 runnable threads, a number nobody chose and no image here pins. That is
`servers/calc`'s lesson arriving by a different route: there a call-counting ceiling of four
admitted sixteen CREST threads on a four-core pod, because `CHEMCLAW_CREST_THREADS` was set and not
counted. Here nothing is set at all, which is worse, because the number changes with the node.

So `acquire` takes a **cost**: one slot for the RDKit-only path, which the measurement above shows
is one core's worth however many threads it is given, and `mapping.inference_threads()` where a
mapper is installed — read at call time from torch itself rather than assumed, so a deployment that
pins `OMP_NUM_THREADS` is charged what it actually pinned. A cost above the whole ceiling is
*clamped* to it rather than refused: a pod configured smaller than one batch must run that batch
exclusively, because a tool that can never be admitted is a tool that configuration has deleted.

**Refusing rather than queueing.** A queued batch of 500 comes back after `connector.yaml`'s
`request_timeout` has expired — computed at the expense of one the drain is still waiting for — and
the drain's own answer to a refusal is to come back with the same batch, which is exactly the
behaviour a full pod wants. A `ValueError` is the family `connector_app` passes to the caller
verbatim.

**This is admission control and deliberately not a clock.** Cancelling the awaiting coroutine does
not stop the worker thread, so a per-call wall clock would return an error to a caller who has gone
while the CPU burn continued. Refusing *before* any work starts is the other thing entirely.

The shape is copied from `servers/calc/engine/admission.py` rather than imported — one server never
imports another, and the refusal has to name this server's own tool and knob.
"""

from __future__ import annotations

import threading

__all__ = ["ADMISSION_MARKER", "DEFAULT_MAX_CONCURRENT_BATCHES", "Admission"]

# The attribute `tools._admitted` stamps on a gated tool, and the only thing that tells the coverage
# test which tools are gated. A name rather than a hand-kept list, because the thing that must not
# be forgotten is exactly the thing a forgetful change adds.
ADMISSION_MARKER = "__admission_gated__"

#: How many cores' worth of labelling may be in flight, and **two because
#: `deploy/deployment.yaml` gives this pod `limits.cpu: "2"`**. A slot is a core, so the ceiling is
#: the pod's own allowance: admitting more than it turns every batch slower than it would have been
#: alone without labelling one extra reaction, and admitting fewer leaves a core idle while the
#: drain is refused. Overridable with `CHEMCLAW_RXNLABEL_MAX_CONCURRENT_BATCHES`, read in
#: `tools.py`; the number lives here beside the argument for it, and `tests/test_admission.py`
#: reads the shipped Deployment so a change to `limits.cpu` lands on an assertion rather than on
#: this sentence.
DEFAULT_MAX_CONCURRENT_BATCHES = 2

#: How many reactions one request may carry. The transport already caps the request *body* in
#: bytes, and ten thousand one-line reactions are comfortably under that cap and minutes of
#: transformer time — a timeout the caller reads as an outage rather than as "ask for less".
#: Measured on the RDKit-only path at 2.8 ms/reaction, so 500 is 1.4 s of one core before a mapper
#: is installed and more after. Overridable with `CHEMCLAW_RXNLABEL_MAX_BATCH`, read in `tools.py`;
#: the number lives here beside the argument for it, and beside the ceiling it is priced against.
DEFAULT_MAX_BATCH = 500


class Admission:
    """A budget of concurrent labelling slots, refused rather than queued past it.

    Guarded by a lock rather than an `asyncio.Semaphore`: slots are taken on the event loop and
    given back from whichever thread or callback finishes the work, and nothing ever waits — a full
    gate is an immediate refusal, so nothing here ever suspends.

    A slot is one core's worth of work: one for the RDKit path, which is GIL-bound and measured
    serial, and whatever the mapper's transformer is configured to spend where one is installed.
    """

    def __init__(self, limit: int) -> None:
        """Args: limit: the most slots that may be held at once. Must be at least one."""
        if limit < 1:
            raise ValueError(f"an admission ceiling of {limit} would refuse every batch")
        self._limit = limit
        self._lock = threading.Lock()
        self._in_flight = 0

    @property
    def limit(self) -> int:
        """The configured ceiling, in slots."""
        return self._limit

    @property
    def in_flight(self) -> int:
        """How many slots are held right now."""
        with self._lock:
            return self._in_flight

    def acquire(self, what: str, cost: int = 1) -> int:
        """Take `cost` slots, or refuse in terms the caller can act on.

        Args:
            what: The tool being asked for, named in the refusal — the caller's levers are which
                tool it called, how large a batch it sent, and when, so the message has to say
                which one was turned away.
            cost: How many slots this call occupies. Clamped into `1..limit`: a cost of zero would
                make a tool uncounted, and a cost above the ceiling would make it permanently
                unadmittable, so the expensive one takes the pod exclusively instead.

        Returns:
            The slots actually taken, which is what `release` must be given back — the clamp means
            that is not always `cost`.

        Raises:
            ValueError: the budget does not have room. Worded for whoever receives it: Chemclaw3's
                labelling drain, which can back off and re-send the identical batch, or an agent
                reading a tool error.
        """
        charge = max(1, min(cost, self._limit))
        with self._lock:
            if self._in_flight + charge > self._limit:
                free = self._limit - self._in_flight
                raise ValueError(
                    f"this server has {free} of its {self._limit} labelling slots free and "
                    f"{what} needs {charge}, so it was refused rather than queued: a slot is one "
                    "core, a maximal batch is seconds to minutes of CPU, and a queued one would "
                    "come back after the caller had stopped waiting for it. Re-send the identical "
                    "batch once one finishes, or send a smaller one; raise "
                    "CHEMCLAW_RXNLABEL_MAX_CONCURRENT_BATCHES only on a pod with more cores, "
                    "because labelling is CPU-bound and a wider ceiling on the same pod makes "
                    "every batch slower without labelling one extra reaction"
                )
            self._in_flight += charge
        return charge

    def release(self, cost: int = 1) -> None:
        """Give `cost` slots back. Never below zero, so one double release cannot open the gate."""
        with self._lock:
            self._in_flight = max(0, self._in_flight - cost)
