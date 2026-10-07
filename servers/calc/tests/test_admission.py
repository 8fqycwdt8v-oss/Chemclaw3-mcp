"""How much this pod will run at once, and what it does with the call that arrives when it is full.

Heavy tools offload to worker threads and each in-process calculation is one core
(`OMP_NUM_THREADS=1`), so an unbounded burst thrashes and retries pile on. Properties:

- **The refusal is prompt.** A full pod refuses before any work starts, rather than queueing.
- **The slot outlives a caller that gave up.** Releasing on cancellation would hand the slot to a
  retry while the original thread still burns.
- **The cheap tools stay answerable**, notably `calculation_key`.
- **A slot is a core, not a call.** CREST runs `CHEMCLAW_CREST_THREADS` threads and is charged
  them.

The gated set is checked against `connector.yaml`'s `state_changing` list, not a list kept here.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from chemclaw_mcp_calc import tools
from chemclaw_mcp_calc.engine.admission import (
    ADMISSION_MARKER,
    AT_CAPACITY_MARKER,
    Admission,
    AtCapacityError,
)
from chemclaw_mcp_calc.engine.config import CalcSettings, settings
from chemclaw_mcp_calc.engine.structure import Structure
from mcp_server_kit.testing import load_manifest

MANIFEST = Path(__file__).resolve().parents[1] / "connector.yaml"


@pytest.fixture
def one_slot(monkeypatch: pytest.MonkeyPatch) -> Iterator[Admission]:
    """Run the server's gate at a ceiling of one, so "full" is one call rather than four."""
    gate = Admission(1)
    monkeypatch.setattr(tools, "_admission", gate)
    yield gate


class _BlockingCalculation:
    """An engine call that parks its worker thread until the test lets it finish."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.finish = threading.Event()
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> str:
        self.calls += 1
        self.started.set()
        assert self.finish.wait(30), "the test never released the blocked calculation"
        return "done"


async def _settle() -> None:
    """Let the loop run the done-callback that returns a slot."""
    for _ in range(100):
        await asyncio.sleep(0.01)
        if tools._admission.in_flight == 0:
            return


def test_the_gate_refuses_once_the_ceiling_is_reached_and_reopens_when_one_finishes() -> None:
    """The mechanism alone: two slots, a third caller refused in terms it can act on."""
    gate = Admission(2)
    gate.acquire("a compute_hessian")
    gate.acquire("a compute_hessian")
    assert gate.in_flight == 2

    with pytest.raises(ValueError, match="0 of its 2 calculation slots free") as refusal:
        gate.acquire("a compute_hessian")
    # The refusal has to name the lever, not just the fact: a caller cannot act on "busy".
    assert "CHEMCLAW_CALC_MAX_CONCURRENT_REQUESTS" in str(refusal.value)

    gate.release()
    gate.acquire("a compute_hessian")
    assert gate.in_flight == 2


def test_a_saturation_refusal_is_typed_and_marked_rather_than_worded() -> None:
    """Saturation and a bad molecule must be separable by something other than reading English.

    The type (`AtCapacityError`) is for this repository and the marker is for the wire, at the head
    of the message because FastMCP prefixes it. It must be a `ValueError`, the family the sanitiser
    passes through as caller-safe.
    """
    gate = Admission(1)
    gate.acquire("a search_conformer_ensemble")

    with pytest.raises(AtCapacityError) as refusal:
        gate.acquire("a compute_hessian")

    assert isinstance(refusal.value, ValueError)
    assert str(refusal.value).startswith(AT_CAPACITY_MARKER)
    # The literal, transcribed. Chemclaw3 holds its own copy of this string with no shared package
    # between the two repositories to import from, so the pair is only kept honest by each side
    # pinning the spelling it expects rather than the constant it defines.
    assert AT_CAPACITY_MARKER == "[calc-at-capacity]"


def test_a_ceiling_below_one_is_refused_at_construction() -> None:
    """A gate that admits nothing is a misconfiguration, not a very strict policy."""
    with pytest.raises(ValueError, match="would refuse every calculation"):
        Admission(0)


async def test_a_full_pod_refuses_the_next_calculation_before_starting_it(
    one_slot: Admission, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal is *admission*: the second call never reaches the engine at all."""
    blocking = _BlockingCalculation()
    monkeypatch.setattr(tools, "run_xtb", blocking)

    first = asyncio.ensure_future(tools.compute_xtb_energy("CCO"))
    await asyncio.to_thread(blocking.started.wait, 30)
    assert one_slot.in_flight == 1

    with pytest.raises(ValueError, match="0 of its 1 calculation slots free"):
        await tools.compute_xtb_energy("CCO")
    assert blocking.calls == 1, (
        "the refused call still reached the engine: it was queued behind a running calculation "
        "rather than shed at admission, which is the control this gate is not"
    )

    blocking.finish.set()
    assert await first == "done"
    await _settle()
    assert one_slot.in_flight == 0


async def test_the_slot_is_held_until_the_work_finishes_not_until_the_caller_gives_up(
    one_slot: Admission, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The slot is held until the work finishes, not until the caller gives up.

    Cancelling the awaiting coroutine does not stop the worker thread, so releasing on cancellation
    would admit a retry beside a calculation still burning a core.
    """
    blocking = _BlockingCalculation()
    monkeypatch.setattr(tools, "run_xtb", blocking)

    call = asyncio.ensure_future(tools.compute_xtb_energy("CCO"))
    await asyncio.to_thread(blocking.started.wait, 30)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    await asyncio.sleep(0.05)
    assert one_slot.in_flight == 1, (
        "the caller's cancellation returned the slot while its worker thread was still running: a "
        "retry would now be admitted beside a calculation that never stopped"
    )
    with pytest.raises(ValueError, match="0 of its 1 calculation slots free"):
        await tools.compute_xtb_energy("CCO")

    blocking.finish.set()
    await _settle()
    assert one_slot.in_flight == 0


async def test_the_tools_that_run_no_scf_stay_answerable_while_the_pod_is_full(
    one_slot: Admission, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`calculation_key` is what a client calls to *avoid* work; refusing it adds work."""
    blocking = _BlockingCalculation()
    monkeypatch.setattr(tools, "run_xtb", blocking)

    running = asyncio.ensure_future(tools.compute_xtb_energy("CCO"))
    await asyncio.to_thread(blocking.started.wait, 30)

    identity = await tools.calculation_key("compute_xtb_energy", {"smiles": "CCO"})
    assert identity.calc_version

    blocking.finish.set()
    await running
    await _settle()


WATER = Structure(
    elements=[8, 1, 1], positions=[[0.0, 0.0, 0.0], [0.96, 0.0, 0.0], [-0.24, 0.93, 0.0]]
)


async def test_a_crest_search_is_charged_its_threads_rather_than_one_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CREST search is charged its threads rather than one call.

    `crest_cli._environment()` scrubs the child environment and sets `-T`/`OMP_NUM_THREADS` from
    `CHEMCLAW_CREST_THREADS` (4 shipped), so counting calls would oversubscribe the pod. Charged its
    threads, one search fills a four-slot pod and the next call is refused.
    """
    gate = Admission(4)
    monkeypatch.setattr(tools, "_admission", gate)
    monkeypatch.setattr(settings, "crest_threads", 4)
    blocking = _BlockingCalculation()
    monkeypatch.setattr(tools, "_ensemble_payload", blocking)
    monkeypatch.setattr(tools, "run_xtb", _BlockingCalculation())

    search = asyncio.ensure_future(tools.search_conformer_ensemble(WATER))
    await asyncio.to_thread(blocking.started.wait, 30)
    assert gate.in_flight == 4, (
        f"one CREST search holds {gate.in_flight} of 4 slots while running 4 threads: the ceiling "
        "is counting calls, so four of these would be admitted together at the shipped default"
    )

    with pytest.raises(ValueError, match="0 of its 4 calculation slots free"):
        await tools.compute_xtb_energy("CCO")

    blocking.finish.set()
    await search
    await _settle()
    assert gate.in_flight == 0, "the search gave back fewer slots than it took"


async def test_an_unpinned_crest_search_takes_the_whole_pod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`crest_threads = 0` means the sampler sizes itself from the node, so the search runs alone.

    The charge is capped at the budget, so a ceiling below one search's threads does not make the
    tool permanently unadmittable.
    """
    gate = Admission(2)
    monkeypatch.setattr(tools, "_admission", gate)
    monkeypatch.setattr(settings, "crest_threads", 0)
    blocking = _BlockingCalculation()
    monkeypatch.setattr(tools, "_ensemble_payload", blocking)

    search = asyncio.ensure_future(tools.search_binding_modes(WATER))
    await asyncio.to_thread(blocking.started.wait, 30)
    assert gate.in_flight == 2

    blocking.finish.set()
    await search
    await _settle()
    assert gate.in_flight == 0

    # And the clamp again from the other side: a search wider than the whole pod is still admitted
    # onto an empty one, because a tool that can never be admitted has been deleted by config.
    monkeypatch.setattr(settings, "crest_threads", 64)
    assert gate.acquire("a search_binding_modes", tools._crest_slots()) == 2


def test_every_state_changing_tool_is_gated_and_no_read_only_one_is() -> None:
    """The manifest's own classification is the rule, so a new tool cannot be gated by accident.

    Read off the served surface, so a `state_changing` tool left ungated fails here.
    """
    endpoint = load_manifest(MANIFEST).endpoint
    state_changing = set(endpoint.state_changing)
    read_only = set(endpoint.read_only)

    manager = tools.server._tool_manager
    served = {tool.name for tool in asyncio.run(tools.server.list_tools())}
    registered = {name: manager.get_tool(name) for name in served}
    assert all(tool is not None for tool in registered.values())
    gated = {
        name
        for name, tool in registered.items()
        if tool is not None and getattr(tool.fn, ADMISSION_MARKER, False)
    }

    assert gated == state_changing, (
        f"ungated calculations: {sorted(state_changing - gated)}; "
        f"gated tools the manifest calls read_only: {sorted(gated - state_changing)}"
    )
    assert not (gated & read_only)
    # Cheapness is not the rule: these two run no SCF and are gated anyway, because both this
    # manifest and Chemclaw3's classify them `state_changing`.
    assert {"predict_solubility", "predict_developability_profile"} <= gated


def test_the_ceiling_is_an_environment_variable_and_not_a_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This pod's ceiling is settable from outside the image, though it reads like a constant.

    `CalcSettings` has `env_prefix="CHEMCLAW_"`, so `CHEMCLAW_CALC_MAX_CONCURRENT_REQUESTS` (and the
    `xtb_max_atoms` bound) can be moved by a Deployment `env:` entry. The fleet ratchet must
    therefore cover this server too. Asserted by constructing the settings twice and seeing the
    numbers differ.
    """
    default = CalcSettings()
    assert (default.calc_max_concurrent_requests, default.xtb_max_atoms) == (4, 450)

    monkeypatch.setenv("CHEMCLAW_CALC_MAX_CONCURRENT_REQUESTS", "99")
    monkeypatch.setenv("CHEMCLAW_XTB_MAX_ATOMS", "99999")
    widened = CalcSettings()
    assert (widened.calc_max_concurrent_requests, widened.xtb_max_atoms) == (99, 99999)
