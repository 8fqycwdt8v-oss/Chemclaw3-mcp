"""The semi-batch integrator's ceiling, and the offload without which a ceiling means nothing.

`engine/admission.py` has the measurement. Five properties, each with its own failure:

- **The integration runs off the event loop.** FastMCP 1.x calls a synchronous tool on the loop, so
  before this the worst legal dose stopped every other request — `/healthz` included — for seconds,
  and a ceiling on it could never trip because two could never be in flight.
- **A full pod refuses promptly**, before any integration starts.
- **The slot outlives a caller that gave up**, because cancelling the awaiting coroutine does not
  stop the worker thread.
- **Only the integrator is gated**, derived from the served surface.
- **The ceiling fits the caller's budget**, read from `connector.yaml` rather than transcribed.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from chemclaw_mcp_kinetics import tools
from chemclaw_mcp_kinetics.engine import reactors
from chemclaw_mcp_kinetics.engine.admission import (
    ADMISSION_MARKER,
    DEFAULT_MAX_CONCURRENT_INTEGRATIONS,
    WORST_INTEGRATION_SECONDS,
    Admission,
)
from mcp_server_kit.testing import reimported

MANIFEST = Path(__file__).resolve().parents[1] / "connector.yaml"

#: What the blocking stand-in holds a slot for, and the yardstick a refusal is measured against.
BLOCK_SECONDS = 2.0

_DOSE: dict[str, float] = {
    "rate_constant": 0.02,
    "dose_time_seconds": 7200.0,
    "initial_volume": 50.0,
    "dosed_moles": 40.0,
    "initial_coreagent_concentration": 0.9,
}


class _BlockingIntegration:
    """A stand-in for `reactors.semibatch_accumulation` that holds its thread until released."""

    def __init__(self) -> None:
        """Two events, the threads it ran on, and a peak concurrency."""
        self.started = threading.Event()
        self.finish = threading.Event()
        self.threads: list[int] = []
        self.peak = 0
        self._live = 0
        self._lock = threading.Lock()
        self._real = reactors.semibatch_accumulation

    def __call__(self, **kwargs: Any) -> reactors.SemiBatchProfile:
        """Record the thread, block until released, then answer with the real integration."""
        with self._lock:
            self._live += 1
            self.peak = max(self.peak, self._live)
            self.threads.append(threading.get_ident())
        self.started.set()
        self.finish.wait(30)
        with self._lock:
            self._live -= 1
        return self._real(**kwargs)


async def _settle() -> None:
    """Let the shielded task's done-callback run, which is what gives a slot back."""
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.fixture
def one_slot(monkeypatch: pytest.MonkeyPatch) -> Iterator[Admission]:
    """Run the server's gate at a ceiling of one, so "full" is one integration."""
    gate = Admission(1)
    monkeypatch.setattr(tools, "_admission", gate)
    yield gate


@pytest.fixture
def blocking(monkeypatch: pytest.MonkeyPatch) -> _BlockingIntegration:
    """Swap the integrator for the blocking stand-in, as `tools` reaches it."""
    stand_in = _BlockingIntegration()
    monkeypatch.setattr(reactors, "semibatch_accumulation", stand_in)
    return stand_in


def test_a_ceiling_below_one_is_refused_at_construction() -> None:
    """A ceiling of zero would refuse every integration, which is a misconfiguration."""
    with pytest.raises(ValueError, match="would refuse every integration"):
        Admission(0)


async def test_the_integration_runs_off_the_event_loop(
    blocking: _BlockingIntegration,
) -> None:
    """The defect under the missing ceiling: a synchronous tool runs on the loop in FastMCP 1.x."""
    blocking.finish.set()
    loop_thread = threading.get_ident()
    result = await tools.semibatch_accumulation_profile(**_DOSE)
    assert result.peak_accumulation_fraction > 0.0
    assert blocking.threads, "the integrator was never reached"
    assert blocking.threads[0] != loop_thread, (
        "semibatch_accumulation ran on the event loop thread: the to_thread hop is gone, and the "
        "worst legal dose stops every other request and the readiness probe for seconds"
    )


async def test_a_full_pod_refuses_the_next_integration_before_starting_it(
    one_slot: Admission, blocking: _BlockingIntegration
) -> None:
    """One integration in flight, the next refused promptly and never reaching a worker thread."""
    running = asyncio.ensure_future(tools.semibatch_accumulation_profile(**_DOSE))
    await asyncio.to_thread(blocking.started.wait, 30)
    assert one_slot.in_flight == 1

    started = time.perf_counter()
    with pytest.raises(ValueError, match="already integrating 1 semi-batch doses") as refused:
        await tools.semibatch_accumulation_profile(**_DOSE)
    assert time.perf_counter() - started < BLOCK_SECONDS / 4, "a slow refusal is a queue"
    assert "this server scales by replicas" in str(refused.value)

    blocking.finish.set()
    await running
    await _settle()
    assert one_slot.in_flight == 0
    assert blocking.peak == 1, "a refused call reached the integrator anyway"


async def test_a_call_its_signature_refuses_leaves_the_ceiling_where_it_was(
    one_slot: Admission, blocking: _BlockingIntegration
) -> None:
    """A malformed call must not cost a slot, or one of them turns a ceiling of one into an outage.

    The gate used to charge before it built the coroutine, and calling an `async def` binds its
    arguments on the spot, so the `TypeError` escaped between the charge and the only code that
    gives a slot back. Driven before the fix: `in_flight` stayed at 1 and the next well-formed
    dose was refused as if an integration were running.
    """
    with pytest.raises(TypeError):
        await tools.semibatch_accumulation_profile(**_DOSE, not_an_argument=1.0)
    assert one_slot.in_flight == 0, "the malformed call kept its slot"

    blocking.finish.set()
    result = await tools.semibatch_accumulation_profile(**_DOSE)
    assert result.peak_accumulation_fraction > 0.0
    await _settle()
    assert one_slot.in_flight == 0


async def test_the_slot_is_held_until_the_integration_finishes_not_until_the_caller_gives_up(
    one_slot: Admission, blocking: _BlockingIntegration
) -> None:
    """Releasing on cancellation would admit a retry beside a worker thread still integrating."""
    running = asyncio.ensure_future(tools.semibatch_accumulation_profile(**_DOSE))
    await asyncio.to_thread(blocking.started.wait, 30)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    await _settle()
    assert one_slot.in_flight == 1, "the slot came back while the worker thread was still running"

    blocking.finish.set()
    for _ in range(300):
        await asyncio.sleep(0.01)
        if one_slot.in_flight == 0:
            break
    assert one_slot.in_flight == 0, "the slot never came back once the integration finished"


async def test_the_closed_form_tools_stay_answerable_while_the_pod_is_full(
    one_slot: Admission, blocking: _BlockingIntegration
) -> None:
    """Only the integrator is heavy; refusing the algebra because of it would be an outage."""
    running = asyncio.ensure_future(tools.semibatch_accumulation_profile(**_DOSE))
    await asyncio.to_thread(blocking.started.wait, 30)
    answer = tools.batch_conversion_after(
        rate_constant=0.01, initial_concentration=1.0, time_seconds=60.0, order=1.0
    )
    assert 0.0 < answer.conversion < 1.0
    blocking.finish.set()
    await running
    await _settle()


def test_only_the_integrator_is_gated_and_it_is_gated() -> None:
    """Derived from the served surface, so a heavy tool added later is gated or this fails."""
    served = {tool.name: tool.fn for tool in tools.server._tool_manager.list_tools()}
    gated = {name for name, fn in served.items() if getattr(fn, ADMISSION_MARKER, False)}
    assert gated == {"semibatch_accumulation_profile"}
    for name in gated:
        assert inspect.iscoroutinefunction(served[name]), (
            f"{name} is gated but synchronous, so FastMCP runs it on the loop and the gate never "
            "sees two in flight"
        )


def test_the_ceiling_fits_inside_the_callers_budget() -> None:
    """N serialised worst cases must finish inside half of the manifest's `request_timeout`."""
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    budget = float(manifest["endpoint"]["request_timeout"])
    derived = math.floor(budget / (2 * WORST_INTEGRATION_SECONDS))
    assert derived == DEFAULT_MAX_CONCURRENT_INTEGRATIONS, (
        f"the ceiling is no longer floor({budget:g} / (2 x {WORST_INTEGRATION_SECONDS:g} s)): the "
        "manifest's request_timeout or the measured worst case moved without the ceiling"
    )
    assert DEFAULT_MAX_CONCURRENT_INTEGRATIONS >= 1


def test_the_ceiling_is_an_environment_variable_and_the_shipped_gate_uses_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read off the module under two environments, so the served gate is the one under test."""
    monkeypatch.delenv("CHEMCLAW_KINETICS_MAX_CONCURRENT_INTEGRATIONS", raising=False)
    assert reimported(tools)._admission.limit == DEFAULT_MAX_CONCURRENT_INTEGRATIONS
    monkeypatch.setenv("CHEMCLAW_KINETICS_MAX_CONCURRENT_INTEGRATIONS", "2")
    assert reimported(tools)._admission.limit == 2
