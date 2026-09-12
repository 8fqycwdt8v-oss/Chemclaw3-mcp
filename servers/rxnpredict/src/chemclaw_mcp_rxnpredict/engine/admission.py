"""How much of this pod may be predicting at once — shed at admission, never queued.

This is the heaviest server in the fleet by memory (`deploy/deployment.yaml` requests 2Gi and
limits 4Gi) and until this gate nothing counted how much of it was in flight. Every predictor's
`predict` hands its forward pass to `asyncio.to_thread` and awaits it, so the only ceiling was
whatever the caller happened to send.

**One tool call is not one thread, and that is the finding this file exists for.**
`predict_forward_reaction` and `predict_reaction_conditions` are *ensembles*: they
`asyncio.gather` over every enabled predictor, each of which offloads separately. Measured against
six deterministic doubles that each sleep, driven through the real tool: one call finished in
0.468 s where serial would have been 2.4 s, with **six worker threads in flight simultaneously**.
So a call-counting ceiling of two on this server admits two times the predictor count, which is a
number the *deployment's* enabled-model list decides and the ceiling never sees.

**And each of those threads is itself wider than one core.** These predictors are torch models, and
torch's intra-op width is `torch.get_num_threads()` — sized from the machine's physical cores, not
from the container's cgroup, and pinned by no image in this fleet. On a 64-core node a pod limited
to two cores gives one forward pass 64 runnable threads. That is `servers/calc`'s lesson arriving
twice over: there a call-counting ceiling of four admitted sixteen CREST threads on a four-core pod
because `CHEMCLAW_CREST_THREADS` was set and not counted; here the fan-out and the thread width are
both uncounted, and the second of them is a number nobody chose.

So `acquire` takes a **cost** in slots, where a slot is a core:

- an ensemble call costs `enabled predictors x inference_threads()` — computed from the
  deployment's own enabled list rather than from the caller's `models` argument, deliberately, so
  the charge cannot be lowered by a caller and is the same for every caller of that tool;
- a single-model call costs `inference_threads()`;
- a cost above the whole ceiling is **clamped** to it rather than refused, so an ensemble wider
  than the pod runs exclusively instead of being a tool configuration has deleted.

At the shipped ceiling that makes a full ensemble exclusive on this pod, which is the intended
answer rather than a side effect: two ensembles over five models is ten torch forward passes on two
cores, every one of them slower than it would have been alone, and the second caller's answer is
not worth the first caller's latency.

**What is outside this gate is what a caller needs answerable while the pod is full.**
`list_available_models` is how a client learns which predictors this build has — refusing it under
load would leave a caller unable to find out *why* it was refused — and `classify_reaction` is a
SMARTS match with no offload at all. Both are `def` rather than `async def`, which is the
structural reason as well as the operational one: nothing they do reaches a worker thread.
`tests/test_admission.py` checks the gated set against the *served surface* rather than against a
list kept here, so a predicting tool added next year is gated or the suite says so.

**Refused rather than queued**, for the reason `servers/calc` and `servers/chem` give: a prediction
held at the back of a queue comes back after `connector.yaml`'s `request_timeout` has expired,
computed at the expense of one somebody is still waiting for. A `ValueError` is the family
`connector_app` passes to the caller verbatim, so the model is told the pod is full rather than
being told its chemistry was the problem.

**This is admission control and deliberately not a clock.** Cancelling the awaiting coroutine does
not stop the worker thread, so a per-call wall clock returns an error to a caller who has gone
while the CPU burn continues. Refusing before any work starts is the other thing entirely.

The shape is copied from `servers/calc/engine/admission.py` rather than imported — one server never
imports another, and the refusal has to name this server's own tool and knob.
"""

from __future__ import annotations

import threading

__all__ = ["ADMISSION_MARKER", "DEFAULT_MAX_CONCURRENT_PREDICTIONS", "Admission"]

# The attribute `tools._admitted` stamps on a gated tool, and the only thing that tells the coverage
# test which tools are gated. A name rather than a hand-kept list, because the thing that must not
# be forgotten is exactly the thing a forgetful change adds.
ADMISSION_MARKER = "__admission_gated__"

#: How many cores' worth of inference may be in flight, and **two because
#: `deploy/deployment.yaml` gives this pod `limits.cpu: "2"`**. A slot is a core, so the ceiling is
#: the pod's own allowance: admitting past it makes every prediction slower than it would have been
#: alone without producing one extra answer, and admitting fewer leaves a core idle while a caller
#: is refused. Overridable with `CHEMCLAW_RXNPREDICT_MAX_CONCURRENT_PREDICTIONS`, read in
#: `tools.py`; the number lives here beside the argument for it, and `tests/test_admission.py`
#: reads the shipped Deployment so a change to `limits.cpu` lands on an assertion rather than on
#: this sentence.
DEFAULT_MAX_CONCURRENT_PREDICTIONS = 2


class Admission:
    """A budget of concurrent inference slots, refused rather than queued past it.

    Guarded by a lock rather than an `asyncio.Semaphore`: slots are taken on the event loop and
    given back from whichever thread or callback finishes the work, and nothing ever waits — a full
    gate is an immediate refusal, so nothing here ever suspends.

    A slot is one core's worth of work. What a call costs is the module docstring's argument: the
    fan-out width times the configured intra-op thread width, never a call count.
    """

    def __init__(self, limit: int) -> None:
        """Args: limit: the most slots that may be held at once. Must be at least one."""
        if limit < 1:
            raise ValueError(f"an admission ceiling of {limit} would refuse every prediction")
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
                tool it called and when, so the message has to say which one was turned away.
            cost: How many slots this call occupies. Clamped into `1..limit`: a cost of zero would
                make a tool uncounted, and a cost above the ceiling would make it permanently
                unadmittable, so a wide ensemble takes the pod exclusively instead.

        Returns:
            The slots actually taken, which is what `release` must be given back — the clamp means
            that is not always `cost`.

        Raises:
            ValueError: the budget does not have room. Worded for whoever receives it — an agent
                reading a tool error, or Chemclaw3 backing off.
        """
        charge = max(1, min(cost, self._limit))
        with self._lock:
            if self._in_flight + charge > self._limit:
                free = self._limit - self._in_flight
                raise ValueError(
                    f"this server has {free} of its {self._limit} inference slots free and "
                    f"{what} needs {charge}, so it was refused rather than queued: a slot is one "
                    "core, an ensemble runs every enabled predictor at once, and a queued "
                    "prediction would come back after the caller had stopped waiting for it. "
                    "Retry once one finishes, or ask a single model with "
                    "predict_forward_single_model / predict_conditions_single_model, which costs "
                    "this pod less. Raising CHEMCLAW_RXNPREDICT_MAX_CONCURRENT_PREDICTIONS only "
                    "helps on a pod with more cores"
                )
            self._in_flight += charge
        return charge

    def release(self, cost: int = 1) -> None:
        """Give `cost` slots back. Never below zero, so one double release cannot open the gate."""
        with self._lock:
            self._in_flight = max(0, self._in_flight - cost)
