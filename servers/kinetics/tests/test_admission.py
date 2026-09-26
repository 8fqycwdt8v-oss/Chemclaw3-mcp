"""The semi-batch integrator's admission ceiling, driven rather than read.

The integrator's step count is derived from the caller's rate constant, so its cost is the caller's
to set — up to half a second of pure-Python RK4 at `MAX_INTEGRATION_STEPS`. Three properties, each
with its own failure: a full pod refuses promptly rather than queueing, the slot outlives a caller
that gave up (cancelling the await does not stop the worker thread), and the work runs off the
event loop so `/healthz` keeps answering.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
from chemclaw_mcp_kinetics import tools
from chemclaw_mcp_kinetics.engine import reactors
from chemclaw_mcp_kinetics.engine.admission import DEFAULT_MAX_CONCURRENT_INTEGRATIONS, Admission

_DOSE = {
    "rate_constant": 0.02,
    "dose_time_seconds": 7200.0,
    "initial_volume": 50.0,
    "dosed_moles": 40.0,
    "initial_coreagent_concentration": 0.9,
    "dosed_volume": 8.0,
}


class _Blocking:
    """A stand-in integrator that holds its worker thread until told to finish."""

    def __init__(self) -> None:
        """One event per direction, so a test can see a worker start and can release it."""
        self.started = threading.Event()
        self.finish = threading.Event()
        self.real = reactors.semibatch_accumulation

    def __call__(self, **kwargs: Any) -> reactors.SemiBatchProfile:
        """Block, then answer with the real integration so the tool can build its result."""
        self.started.set()
        self.finish.wait(30)
        return self.real(**kwargs)


@pytest.fixture
def blocking(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Blocking]:
    """Swap the integrator for the blocking stand-in and give every test a fresh one-slot gate."""
    stand_in = _Blocking()
    monkeypatch.setattr(reactors, "semibatch_accumulation", stand_in)
    monkeypatch.setattr(tools, "_admission", Admission(1))
    yield stand_in
    stand_in.finish.set()


def test_the_default_ceiling_is_the_one_the_gate_was_built_from() -> None:
    """With no override in the environment, the gate enforces the module's documented default."""
    assert tools._admission.limit == DEFAULT_MAX_CONCURRENT_INTEGRATIONS


async def test_a_full_pod_refuses_promptly_rather_than_queueing(blocking: _Blocking) -> None:
    """The second call is turned away while the first holds the only slot."""
    first = asyncio.ensure_future(tools.semibatch_accumulation_profile(**_DOSE))
    await asyncio.to_thread(blocking.started.wait, 5)

    started = time.perf_counter()
    with pytest.raises(ValueError, match="refused rather than queued"):
        await tools.semibatch_accumulation_profile(**_DOSE)
    assert time.perf_counter() - started < 0.5

    blocking.finish.set()
    result = await first
    assert result.peak_accumulation_fraction > 0.0
    assert tools._admission.in_flight == 0


async def test_the_slot_is_held_until_the_work_ends_not_until_the_caller_leaves(
    blocking: _Blocking,
) -> None:
    """A cancelled caller must not free a slot its worker thread is still burning."""
    caller = asyncio.ensure_future(tools.semibatch_accumulation_profile(**_DOSE))
    await asyncio.to_thread(blocking.started.wait, 5)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert tools._admission.in_flight == 1, "the slot was released while the thread still ran"

    blocking.finish.set()
    for _ in range(100):
        if tools._admission.in_flight == 0:
            break
        await asyncio.sleep(0.02)
    assert tools._admission.in_flight == 0


async def test_the_integration_does_not_run_on_the_event_loop(blocking: _Blocking) -> None:
    """While an integration is in flight, the loop is still free to answer something else.

    The clock starts before the call is scheduled, and the checks run while the stand-in is still
    held. Timing only what follows `started` could not fail: an integration run on the loop would
    block it for the stand-in's whole hold *before* `started` was observed, then return — so the
    timed section afterwards was fast either way and the test passed, 30 s late. Here that defect
    shows as a call already finished and an elapsed time of about the hold.
    """
    began = time.perf_counter()
    call = asyncio.ensure_future(tools.semibatch_accumulation_profile(**_DOSE))
    await asyncio.to_thread(blocking.started.wait, 5)
    await asyncio.wait_for(asyncio.sleep(0.01), 1.0)
    assert not call.done(), "the integration finished while held, so it ran on the event loop"
    assert time.perf_counter() - began < 5.0, "the event loop was blocked by the integration"
    blocking.finish.set()
    await call
