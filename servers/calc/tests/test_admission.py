"""How much this pod will run at once, and what it does with the call that arrives when it is full.

Every heavy tool here offloads to a worker thread and awaits it, and until this gate nothing counted
how many were in flight. The image pins `OMP_NUM_THREADS=1` for the in-process stack, so one
in-process calculation is one core and a burst is a thrash: every call slower than it would have
been alone, the caller's own timeout firing, and — because `cached_compute` is check-then-act — the
retry starting another identical burn beside the ones already running.

Four properties, each with its own failure:

- **The refusal is prompt.** A full pod turns a call away before any work starts, rather than
  queueing it behind calculations that take minutes. That is admission control and not the wall
  clock `CLAUDE.md` argues against: nothing is abandoned mid-burn, because nothing was started.
- **The slot outlives a caller that gave up.** The half that breaks the retry loop. Releasing on
  cancellation would hand the freed slot straight to the retry while the original thread was
  still burning — the pod would believe it had room it did not have.
- **The cheap tools stay answerable.** `calculation_key` is how a client asks "have I computed this
  already?" *before* paying for it, so refusing it under load would push work onto a saturated pod.
- **A slot is a core, not a call.** The pin above binds this process and is scrubbed out of CREST's
  environment, which is then told `-T`/`OMP_NUM_THREADS` from `CHEMCLAW_CREST_THREADS`. Counting
  calls admitted four searches at the shipped ceiling — sixteen runnable threads on a four-core pod,
  measured at 4.2x the wall clock of one search alone.

The gated set is checked against `connector.yaml`'s own `state_changing` list rather than a list
kept here, for `test_event_loop_offload.py`'s reason: the thing that must not be forgotten is
exactly the thing a forgetful change adds.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from chemclaw_mcp_calc import tools
from chemclaw_mcp_calc.engine.admission import ADMISSION_MARKER, Admission
from chemclaw_mcp_calc.engine.config import settings
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
    """The half that breaks the retry loop.

    Cancelling the awaiting coroutine does not stop the worker thread, so a slot released on
    cancellation would be handed to the retry while the first calculation was still burning a core —
    two calculations on a pod that believes it is running one.
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
    """A slot is a core, and CREST is the one tool here that spends more than one of them.

    The image's `OMP_NUM_THREADS=1` binds this process and provably cannot reach the sampler:
    `crest_cli._environment()` scrubs the child environment to four allow-listed variables and then
    sets `OMP_NUM_THREADS` — and `-T` beside it — from `CHEMCLAW_CREST_THREADS`, 4 in the shipped
    image. So a gate that counted calls admitted four searches at the shipped ceiling of 4: sixteen
    runnable threads on the ~4-core pod that ceiling was sized for. Measured against a stub `crest`
    burning its `-T` count on a 4-core machine, one search took 1.51 s and four together took
    6.3-6.4 s each.

    Charged its threads, one search fills a four-slot pod, and the next call — of any kind — is
    refused rather than admitted onto a machine that has no core left for it.
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
    """`crest_threads = 0` means the sampler sizes itself from `/proc/cpuinfo` — the node's cores.

    There is no honest cost smaller than "all of it" for that case, so the search runs alone. The
    clamp is the other half: a ceiling below one search's thread count must not make the tool
    permanently unadmittable, so the charge is capped at the budget rather than refused forever.
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

    Read off the *served* surface: a tool that exists and is not in either list would already fail
    `tests/test_server.py`, and one that is classified `state_changing` and left ungated here would
    otherwise ship as the one uncounted way to load this pod.
    """
    endpoint = load_manifest(MANIFEST)["endpoint"]
    state_changing = set(endpoint["state_changing"])
    read_only = set(endpoint["read_only"])

    manager = tools.server._tool_manager
    served = {tool.name for tool in asyncio.run(tools.server.list_tools())}
    gated = {name for name in served if getattr(manager.get_tool(name).fn, ADMISSION_MARKER, False)}

    assert gated == state_changing, (
        f"ungated calculations: {sorted(state_changing - gated)}; "
        f"gated tools the manifest calls read_only: {sorted(gated - state_changing)}"
    )
    assert not (gated & read_only)
    # Cheapness is **not** the rule, and this pair is what proves it rather than argues it: both
    # run no SCF — Delaney over RDKit descriptors, and MolWt/MolLogP/TPSA/QED — and both are gated,
    # because both this manifest and Chemclaw3's classify them `state_changing`. The docstring here
    # once said the split was "the three tools that run no SCF", which was wrong on the count (five
    # served tools run none) and inverted on the cost (these two are 1.4 ms and 2.4 ms warm against
    # 10.5 ms for the ungated `calculation_key`). Re-deriving the split from cost would ungate them
    # and break an agreement that spans two repositories.
    assert {"predict_solubility", "predict_developability_profile"} <= gated
