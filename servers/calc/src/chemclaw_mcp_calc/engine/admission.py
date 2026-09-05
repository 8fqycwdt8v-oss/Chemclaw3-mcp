"""How much CPU this server will run at once — shed at admission, never queued.

Every heavy tool here hands its work to `asyncio.to_thread` and awaits it, and nothing counted how
much of that was in flight. So the aggregate load on a pod was whatever its callers happened to
send: the image pins `OMP_NUM_THREADS=1` for the in-process stack, so one *in-process* calculation
is one core, and a burst of twenty therefore thrashes a pod sized for four, every one of them
slower than it would have been alone. That is the shape that feeds itself — each call takes longer,
the caller's own timeout fires, and `cached_compute` is check-then-act, so the retry starts another
identical burn beside the ones already running.

**A slot is a core, not a call, and CREST is the whole reason.** The ceiling used to count calls
and derive its default from that `OMP_NUM_THREADS=1` pin — which provably does not reach the one
tool family that spends more than one core. `crest_cli._environment()` scrubs the child environment
to four allow-listed variables, so the pin cannot be inherited, and then sets `OMP_NUM_THREADS` and
`-T` from `CHEMCLAW_CREST_THREADS` (4 in the shipped image, which the Containerfile states as the
exception: "a CREST run is the one thing in this image that should use more than one core"). Four
admitted searches were therefore sixteen runnable threads on the ~4-core pod the ceiling was sized
for. Measured, driving the real tool against a stub `crest` that burns its `-T` count on a 4-core
machine: one search 1.51 s, two together 3.13 s each, four together 6.3-6.4 s each — a 4.2x
inflation at the shipped default, which is exactly the thrash this file opens by describing.

So `acquire` takes a **cost**: one slot for anything that runs in this process under the pin, and
`crest_threads` for a CREST search, which is the number the sampler is actually told to use. The
budget is then the same quantity end to end — cores — rather than a call count that means different
things for different tools. A cost above the whole ceiling is *clamped* to it rather than refused:
a pod configured smaller than one search must run that search exclusively, because a tool that can
never be admitted is a tool that has been deleted by configuration.

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

**What is outside this gate is the manifest's `read_only` list, and nothing else.**
`calculation_key`, `embed_structure` and `combine_structures` are what a caller needs answerable
*while* the pod is full: `calculation_key` is how a client asks "have I computed this already?"
before paying for it, so refusing it under load would push work onto a pod that is already
saturated. The split is not a judgement re-made per tool and it is deliberately **not** a cost
list — `tests/test_admission.py` checks the gated set against the manifest's own
`read_only`/`state_changing` classification, which is the same list `connector.yaml` argues on
plan-gate grounds and the same one Chemclaw3's `connectors/calc/connector.yaml` carries.

That distinction used to be stated here as "the three tools that run no SCF", which was wrong on
its count and inverted on its cost: five served tools run no SCF — `predict_solubility` (Delaney
over RDKit descriptors) and `predict_developability_profile` are gated and cost 1.4 ms and 2.4 ms
warm, against 10.5 ms for the ungated `calculation_key` and 9.6 ms for `embed_structure`. Both are
`state_changing` in both repositories' manifests, so re-classifying them to match cost would break
that agreement and repurpose a gate list as a cost list. The gate is right; only the justification
was.

**A full pod and a bad molecule are two different answers, and the wire could not tell them apart.**
Everything a tool body raises reaches the caller the same way — FastMCP folds it into a `ToolError`,
`mcp_server_kit` decides it is caller-safe because it is a `ValueError`, and the transport returns
`isError=True` with one text block. There is no error code and no structured payload on that path:
`mcp.server.lowlevel`'s `_make_error_result` builds `CallToolResult(content=[TextContent(...)],
isError=True)` and nothing else, and `Tool.run` has already flattened every exception type into one.
So the *only* channel a refusal has is its own text, and Chemclaw3 was therefore classifying "this
pod is full" as bad data — an unparameterised solvent, an atom index past the molecule — and marking
the durable job non-retryable on its first attempt, carrying this file's own advice to retry.

`AT_CAPACITY_MARKER` is that channel used deliberately rather than accidentally. It is a fixed token
at the head of the message, not a phrase of the sentence around it, so the human wording stays free
to change; `AtCapacityError` is the type this side catches on. It is the same mechanism
`mcp_server_kit` already uses for "an internal error occurred", which Chemclaw3 matches to tell a
fault apart from a refusal — a weak contract, and the only one the protocol offers, which is why
both repositories pin the literal in a test rather than importing a constant across a boundary that
does not exist.

**One honest limit.** The slot is held for as long as the awaiting coroutine lives, so a client that
disconnects mid-calculation releases it while its worker thread is still running. The alternative —
holding the slot until the thread finishes — is what this does, by keeping the inner task alive
through `asyncio.shield` and releasing on *its* completion rather than on the awaiting caller's; see
`tools._admitted`. What that cannot cover is the process dying, which needs no accounting.
"""

from __future__ import annotations

import threading

__all__ = ["ADMISSION_MARKER", "AT_CAPACITY_MARKER", "Admission", "AtCapacityError"]

# The attribute `tools._admitted` stamps on a gated tool, and the only thing that tells the coverage
# test which tools are gated. A name rather than a hand-kept list, because the thing that must not
# be forgotten is exactly the thing a forgetful change adds.
ADMISSION_MARKER = "__admission_gated__"

# The token that tells a caller "full pod", not "bad input", across a wire that carries neither an
# error code nor a structured payload for a refused tool call (the module docstring has the
# mechanism). Bracketed and hyphenated so it cannot occur in a sentence anybody writes by accident,
# and placed at the *head* of the message so it survives every wrapping the transport applies —
# FastMCP prefixes `Error executing tool <name>: `, and `mcp_server_kit` may re-wrap after
# redaction. Chemclaw3 transcribes this exact string as its own literal
# (`core/mcp_session.SERVER_AT_CAPACITY`), because there is no shared package between the two
# repositories to import it from; changing it here without changing it there silently restores the
# defect, which is why each side pins it in a test.
AT_CAPACITY_MARKER = "[calc-at-capacity]"


class AtCapacityError(ValueError):
    """This pod is full; the identical call may well succeed once a calculation finishes.

    A `ValueError` so `mcp_server_kit` still treats it as a deliberately worded, caller-safe
    refusal rather than replacing it with an internal-error notice — the family is the contract,
    and narrowing it here would hide the message from the caller entirely. The subclass exists so
    this server's own code and tests can catch saturation precisely instead of matching prose.
    """


class Admission:
    """A budget of concurrent calculation slots, refused rather than queued past it.

    Guarded by a lock rather than an `asyncio.Semaphore`: slots are taken on the event loop and
    given back from whichever thread or callback finishes the work, and nothing ever waits —
    a full gate is an immediate refusal, so nothing here ever suspends.

    A slot is one core's worth of work. Everything that runs in this process costs one, because the
    image pins the numerical stack to a single thread; a CREST search costs what the sampler is
    told to use, which is the module docstring's argument.
    """

    def __init__(self, limit: int) -> None:
        """Args: limit: the most slots that may be held at once. Must be at least one."""
        if limit < 1:
            raise ValueError(f"an admission ceiling of {limit} would refuse every calculation")
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
            what: The calculation being asked for, named in the refusal — the caller's levers are
                which tool it called and when, so the message has to say which one was turned away.
            cost: How many slots this calculation occupies. Clamped into `1..limit`: a cost of zero
                would make a tool uncounted, and a cost above the ceiling would make it permanently
                unadmittable, so the expensive one takes the pod exclusively instead.

        Returns:
            The slots actually taken, which is what `release` must be given back — the clamp means
            that is not always `cost`.

        Raises:
            AtCapacityError: the budget does not have room. A `ValueError`, so it stays a
                caller-safe refusal, and its message opens with `AT_CAPACITY_MARKER` so the
                receiver — Chemclaw3's `cached_compute`, or an agent reading a tool error — can
                tell backpressure from bad data. Worded for that receiver either way.
        """
        charge = max(1, min(cost, self._limit))
        with self._lock:
            if self._in_flight + charge > self._limit:
                free = self._limit - self._in_flight
                raise AtCapacityError(
                    f"{AT_CAPACITY_MARKER} "
                    f"this server has {free} of its {self._limit} calculation slots free and "
                    f"{what} needs {charge}, so it was refused rather than queued: a slot is one "
                    "core, the calculations here are seconds to hours of CPU, and a queued one "
                    "would come back after the caller had stopped waiting for it. Retry once one "
                    "finishes, or raise CHEMCLAW_CALC_MAX_CONCURRENT_REQUESTS on a pod sized for "
                    "more"
                )
            self._in_flight += charge
        return charge

    def release(self, cost: int = 1) -> None:
        """Give `cost` slots back. Never below zero, so one double release cannot open the gate."""
        with self._lock:
            self._in_flight = max(0, self._in_flight - cost)
