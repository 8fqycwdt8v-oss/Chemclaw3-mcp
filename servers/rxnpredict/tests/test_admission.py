"""How much this pod will predict at once, and what it does with the call that arrives when full.

Until this gate, nothing counted how much inference was in flight on the fleet's heaviest server.
The finding that decides the shape of the bound is that **one tool call is not one thread**: the two
consensus tools `asyncio.gather` over every enabled predictor, each of which offloads its own
forward pass, so a ceiling that counted calls would under-count by the deployment's enabled-model
list — a number the ceiling never sees.

Four properties, each with its own failure:

- **A call costs its fan-out.** Measured here rather than argued: one ensemble call over six
  doubles puts six worker threads in flight at once, and finishes in a fraction of what serial
  would take.
- **The refusal is prompt.** A full pod turns a prediction away before any work starts, rather than
  queueing it behind forward passes and answering after `request_timeout` has expired.
- **The slot outlives a caller that gave up.** Cancelling the awaiting coroutine does not stop the
  worker threads, so releasing on cancellation would hand slots to a retry while the originals ran.
- **The two tools that offload nothing stay answerable.** `list_available_models` is how a caller
  finds out what this build has — including why it was refused — and `classify_reaction` is a
  SMARTS match on the event loop.

The gated set is checked against the *served* surface rather than a list kept here: the thing that
must not be forgotten is exactly the thing a forgetful change adds.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from chemclaw_mcp_rxnpredict import tools
from chemclaw_mcp_rxnpredict.engine import config
from chemclaw_mcp_rxnpredict.engine import predictors as registry
from chemclaw_mcp_rxnpredict.engine.admission import (
    ADMISSION_MARKER,
    DEFAULT_MAX_CONCURRENT_PREDICTIONS,
    Admission,
)
from chemclaw_mcp_rxnpredict.engine.cache import reset_cache_for_tests
from chemclaw_mcp_rxnpredict.engine.config import reset_settings_for_tests
from chemclaw_mcp_rxnpredict.engine.predictors.base import BaseForwardPredictor
from chemclaw_mcp_rxnpredict.engine.schemas import ForwardPrediction
from mcp_server_kit.testing import reimported

DEPLOYMENT = Path(__file__).resolve().parents[1] / "deploy" / "deployment.yaml"

REACTANTS = "CC(=O)Cl.Nc1ccccc1"

#: How many doubles the fan-out probe registers. More than the shipped ceiling, so "one call costs
#: its fan-out" is a statement the ceiling cannot satisfy by accident.
FAN_OUT = 6

#: How long each double holds its worker thread. Long enough that six of them running *serially*
#: would be unmistakable against six running together.
BLOCK_SECONDS = 0.4

#: What a refusal may cost as a fraction of the work it would have queued behind.
MAX_REFUSAL_FRACTION = 0.25


class _SlowPredictor(BaseForwardPredictor):
    """A double that occupies a worker thread for `BLOCK_SECONDS` and records the peak overlap."""

    peak = 0
    _live = 0
    _lock = threading.Lock()

    def __init__(self, name: str) -> None:
        """Name it; there is nothing to load and nothing to predict but a fixed product."""
        super().__init__()
        self.name = name
        self.description = "A double that sleeps, so concurrency is visible."
        self.citation = None
        self.extras_install = None

    def load(self) -> None:
        """Nothing to load — which is the point."""
        self._loaded = True

    def predict_sync(self, reactants: str, top_k: int) -> list[ForwardPrediction]:
        """Hold a worker thread, counting how many are held at once across every instance."""
        with _SlowPredictor._lock:
            _SlowPredictor._live += 1
            _SlowPredictor.peak = max(_SlowPredictor.peak, _SlowPredictor._live)
        time.sleep(BLOCK_SECONDS)
        with _SlowPredictor._lock:
            _SlowPredictor._live -= 1
        return [ForwardPrediction(product_smiles="CCO", score=1.0, rank=1, source_model=self.name)]


class _BlockingPredictor(_SlowPredictor):
    """A double that holds its thread until a test releases it."""

    def __init__(self, name: str) -> None:
        """Two events: one the test waits on, one it sets to let the forward pass finish."""
        super().__init__(name)
        self.started = threading.Event()
        self.finish = threading.Event()

    def predict_sync(self, reactants: str, top_k: int) -> list[ForwardPrediction]:
        """Signal that a worker thread got here, then block until released."""
        self.started.set()
        self.finish.wait(30)
        return [ForwardPrediction(product_smiles="CCO", score=1.0, rank=1, source_model=self.name)]


@pytest.fixture
def registry_of(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """A factory that replaces the forward registry with the doubles a test names.

    The registry is process-wide, so it is saved and restored rather than cleared: a test that left
    doubles behind would change what every later test in this server's suite calls a consensus.
    """
    saved_forward = dict(registry._FORWARD)
    saved_conditions = dict(registry._CONDITIONS)
    reset_cache_for_tests()
    reset_settings_for_tests()
    _SlowPredictor.peak = 0
    _SlowPredictor._live = 0

    def _install(*predictors: BaseForwardPredictor) -> None:
        registry._FORWARD.clear()
        registry._CONDITIONS.clear()
        for predictor in predictors:
            registry.register_forward(predictor)

    yield _install

    registry._FORWARD.clear()
    registry._FORWARD.update(saved_forward)
    registry._CONDITIONS.clear()
    registry._CONDITIONS.update(saved_conditions)
    reset_cache_for_tests()
    reset_settings_for_tests()


async def _settle() -> None:
    """Let the shielded task's done-callback run, which is what gives the slots back."""
    for _ in range(20):
        await asyncio.sleep(0)


def test_a_ceiling_below_one_is_refused_at_construction() -> None:
    """A ceiling of zero would refuse every prediction, which is a misconfiguration."""
    with pytest.raises(ValueError, match="would refuse every prediction"):
        Admission(0)


def test_the_gate_refuses_once_the_ceiling_is_reached_and_reopens_when_one_finishes() -> None:
    """The counter itself, with no transport and no event loop in the way."""
    gate = Admission(2)
    assert gate.acquire("a predict_forward_single_model") == 1
    assert gate.acquire("a predict_conditions_single_model") == 1
    with pytest.raises(ValueError, match="0 of its 2 inference slots free"):
        gate.acquire("a predict_forward_reaction")
    gate.release()
    assert gate.acquire("a predict_forward_reaction") == 1


async def test_one_ensemble_call_is_one_worker_thread_per_enabled_predictor(
    registry_of: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measurement the whole cost model rests on, driven through the real tool.

    Six doubles that each hold a worker thread for `BLOCK_SECONDS`: if the ensemble were serial the
    call would take six times that, and if it were one thread the peak would be one. Measured when
    written against six sleeping doubles: **0.468 s against a serial 2.4 s, peak six threads**.

    So a call-counting ceiling of two on this pod admits twelve forward passes, and the multiplier
    is the deployment's own enabled-model list — a number the ceiling would never see.
    """
    registry_of(*(_SlowPredictor(f"slow_{index}") for index in range(FAN_OUT)))
    monkeypatch.setattr(tools, "_admission", Admission(FAN_OUT * 4))

    started = time.perf_counter()
    await tools.predict_forward_reaction(REACTANTS, 3)
    elapsed = time.perf_counter() - started

    assert _SlowPredictor.peak == FAN_OUT, (
        f"one ensemble call put {_SlowPredictor.peak} worker threads in flight against "
        f"{FAN_OUT} enabled predictors"
    )
    assert elapsed < BLOCK_SECONDS * FAN_OUT / 2, (
        f"the ensemble took {elapsed:.3f} s against a serial {BLOCK_SECONDS * FAN_OUT:.1f} s, so "
        "the fan-out this cost model is built on is not happening"
    )
    assert tools._forward_ensemble_slots() == FAN_OUT * config.inference_threads()


async def test_a_full_pod_refuses_the_next_prediction_before_starting_it(
    registry_of: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The driven saturation probe: an ensemble holds the pod, the next call is refused promptly.

    "Promptly" is measured against the work it would have queued behind rather than against a bare
    clock — the forward pass in flight is held open until this test releases it, so a gate that
    queued could not answer at all.
    """
    blocking = _BlockingPredictor("blocking")
    registry_of(blocking)
    gate = Admission(1)
    monkeypatch.setattr(tools, "_admission", gate)

    running = asyncio.ensure_future(tools.predict_forward_reaction(REACTANTS, 3))
    await asyncio.to_thread(blocking.started.wait, 30)
    assert gate.in_flight == 1

    started = time.perf_counter()
    with pytest.raises(ValueError, match="0 of its 1 inference slots free"):
        await tools.predict_forward_single_model("blocking", REACTANTS, 3)
    refusal = time.perf_counter() - started
    assert refusal < BLOCK_SECONDS * MAX_REFUSAL_FRACTION, (
        f"the refusal took {refusal * 1000:.1f} ms while a forward pass held the only slot open "
        "indefinitely; a refusal that takes a measurable share of the work is a queue"
    )

    blocking.finish.set()
    await running
    await _settle()
    assert gate.in_flight == 0


async def test_the_slots_are_held_until_the_work_finishes_not_until_the_caller_gives_up(
    registry_of: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The half that breaks the retry loop — and here a retry would land N threads, not one."""
    blocking = _BlockingPredictor("blocking")
    registry_of(blocking)
    gate = Admission(1)
    monkeypatch.setattr(tools, "_admission", gate)

    running = asyncio.ensure_future(tools.predict_forward_reaction(REACTANTS, 3))
    await asyncio.to_thread(blocking.started.wait, 30)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    await _settle()

    assert gate.in_flight == 1, (
        "the slot came back when the caller gave up, while the forward pass was still running"
    )

    blocking.finish.set()
    for _ in range(300):
        await asyncio.sleep(0.01)
        if gate.in_flight == 0:
            break
    assert gate.in_flight == 0, "the slot never came back once the work finished"


async def test_the_tools_that_offload_nothing_stay_answerable_while_the_pod_is_full(
    registry_of: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`list_available_models` is how a caller learns *why* it was refused; refusing it adds work.

    `classify_reaction` is beside it because it is a SMARTS match on the event loop — there is no
    worker thread for a ceiling on worker threads to protect.
    """
    blocking = _BlockingPredictor("blocking")
    registry_of(blocking)
    monkeypatch.setattr(tools, "_admission", Admission(1))

    running = asyncio.ensure_future(tools.predict_forward_reaction(REACTANTS, 3))
    await asyncio.to_thread(blocking.started.wait, 30)

    assert tools.list_available_models().forward
    assert tools.classify_reaction(REACTANTS, "CC(=O)Nc1ccccc1").reaction_class

    blocking.finish.set()
    await running
    await _settle()


def test_an_ensemble_wider_than_the_pod_takes_it_exclusively_rather_than_forever_refused() -> None:
    """The clamp: a cost above the ceiling runs alone, it is never made unadmittable.

    At the shipped ceiling of two, an ensemble over five predictors is exactly this case — which is
    the intended answer rather than a side effect. Two ensembles over five models is ten forward
    passes on two cores, every one slower than it would have been alone.
    """
    gate = Admission(2)
    assert gate.acquire("a predict_forward_reaction", 30) == 2
    with pytest.raises(ValueError, match="0 of its 2 inference slots free"):
        gate.acquire("a predict_conditions_single_model", 1)
    gate.release(2)
    assert gate.in_flight == 0


def test_a_prediction_is_charged_the_models_threads_rather_than_one_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slot is a core, and torch's intra-op width is the second multiplier a call count misses.

    `torch.get_num_threads()` is sized from the machine's physical cores rather than from the
    container's cgroup, and no image in this fleet pins `OMP_NUM_THREADS`, so on a large node one
    forward pass in a two-core pod is a thread count nobody chose. Driven with a stub `torch`
    rather than the real extra, which no test environment here carries: what is under test is the
    number `inference_threads()` *reads*, and reading a real torch would assert this runner's core
    count instead.
    """
    assert config.inference_threads() == 1, "torch is absent here, so one core is the honest cost"

    stub = types.ModuleType("torch")
    stub.get_num_threads = lambda: 8  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "torch", stub)

    assert config.inference_threads() == 8
    assert tools._single_model_slots() == 8


#: The two served tools that are deliberately ungated. Neither reaches a worker thread, and the
#: first is what a caller needs answerable while the pod is full — it is how a client finds out
#: which predictors this build has, and therefore why a consensus was refused or thin.
UNGATED = {"list_available_models", "classify_reaction"}


def test_every_predicting_tool_is_gated_and_only_the_two_that_offload_nothing_are_not() -> None:
    """Derived from the served surface, so a predictor tool added next year is gated or this fails.

    A hand-kept list of gated names here would be the second declaration this repository refuses
    everywhere else: it would agree with itself while a new tool shipped as the one uncounted way
    to load this pod.
    """
    manager = tools.server._tool_manager
    served = {tool.name for tool in asyncio.run(tools.server.list_tools())}
    gated = {
        name
        for name in served
        if getattr(getattr(manager.get_tool(name), "fn", None), ADMISSION_MARKER, False)
    }
    assert gated == served - UNGATED, (
        f"ungated predicting tools: {sorted(served - UNGATED - gated)}; "
        f"gated tools that offload nothing: {sorted(gated & UNGATED)}"
    )


def test_the_ceiling_is_an_environment_variable_and_not_a_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This pod's ceiling is settable from outside the image, and the gate is what was built.

    Read off the module under two environments rather than re-typed into this file. The version
    this replaces compared `int(os.environ.get(...))` to itself and asserted that `os.environ.get`
    works: hardcoding `tools._admission = Admission(64)` — 32x the pod's cores, the variable
    ignored — left it and 202 other tests green.
    """
    monkeypatch.delenv("CHEMCLAW_RXNPREDICT_MAX_CONCURRENT_PREDICTIONS", raising=False)
    assert reimported(tools)._admission.limit == DEFAULT_MAX_CONCURRENT_PREDICTIONS
    monkeypatch.setenv("CHEMCLAW_RXNPREDICT_MAX_CONCURRENT_PREDICTIONS", "11")
    assert reimported(tools)._admission.limit == 11


def test_the_shipped_gate_enforces_the_shipped_default() -> None:
    """The module-level gate is the one the server serves behind, at the default ceiling.

    Cheap and easy to leave out, and it is what makes every `monkeypatch`ed ceiling in this file
    evidence about the real gate rather than about an `Admission` the tests built for themselves.
    `servers/chem` has had it since its gate was written; this server copied the gate and not the
    test, so until now nothing here read `tools._admission` at all.
    """
    assert isinstance(tools._admission, Admission)
    assert tools._admission.limit == DEFAULT_MAX_CONCURRENT_PREDICTIONS


def test_a_gated_tool_still_advertises_its_real_signature() -> None:
    """`functools.wraps` is load-bearing: without it the tool's schema is `(*args, **kwargs)`.

    FastMCP builds each tool's input schema from `inspect.signature`, which follows `__wrapped__`.
    A gate that quietly replaced every argument name with `kwargs` would be invisible in this
    server's own tests and fatal to the agent reading the schema — and the gate's own docstring is
    the only thing that said so in this server until now.
    """
    schema = asyncio.run(tools.server.list_tools())
    predicted = next(tool for tool in schema if tool.name == "predict_forward_reaction")
    assert set(predicted.inputSchema["properties"]) == {"reactants", "top_k", "models"}


class _RecordingAdmission(Admission):
    """An `Admission` that remembers what each call was charged before charging it."""

    def __init__(self, limit: int) -> None:
        """A gate wide enough that nothing is refused; only the charges are under test."""
        super().__init__(limit)
        self.charges: list[int] = []

    def acquire(self, what: str, cost: int = 1) -> int:
        """Record the charge, then take the slots exactly as the real gate does."""
        self.charges.append(cost)
        return super().acquire(what, cost)


async def test_the_charge_does_not_follow_the_callers_models_argument(
    registry_of: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The most-argued property of this gate, and nothing in this file asserted it.

    `_forward_ensemble_slots` reads `_forward_predictors(None)` — the *deployment's* enabled list —
    rather than the caller's `models`, because a charge a caller can lower by naming one model is a
    ceiling a caller can walk past. The execution path narrows on `models` and the charge does not,
    which is the asymmetry that makes the walk-past possible and the reason both halves are
    asserted here: a version that narrowed neither would pass an assertion about the charge alone
    while having nothing left to protect.

    Mutating `_admitted` to take its cost from `kwargs.get("models")` left all 11 tests in this
    file and all 112 in this server green.
    """
    registry_of(*(_SlowPredictor(f"slow_{index}") for index in range(FAN_OUT)))
    one = ["slow_0"]
    assert [predictor.name for predictor in tools._forward_predictors(one)] == one, (
        "the execution path no longer narrows on `models`, so there is nothing to walk past"
    )
    ensemble = tools._forward_ensemble_slots()
    gate = _RecordingAdmission(ensemble * 4)
    monkeypatch.setattr(tools, "_admission", gate)

    # By keyword first, because that is how the tool is invoked in service — FastMCP unpacks the
    # validated arguments as `**kwargs` — and positionally second, so a charge that read the
    # narrowing off either calling convention is caught. The first of those two is the arm that
    # matters: a mutation taking the cost from `kwargs.get("models")` is invisible to the other.
    await tools.predict_forward_reaction(reactants=REACTANTS, top_k=1, models=one)
    await tools.predict_forward_reaction(REACTANTS, 1, one)
    await tools.predict_forward_reaction(REACTANTS, 1)

    assert gate.charges == [ensemble] * 3, (
        f"naming 1 of {FAN_OUT} enabled models was charged {gate.charges[:2]} slots against "
        f"{gate.charges[2]} for the whole ensemble; the fan-out is the registry's either way, so "
        "a caller could hold the pod at a fraction of what it is running"
    )


def test_the_ceiling_is_the_pods_own_core_count() -> None:
    """A slot is a core, so the default has to equal `limits.cpu` in the shipped Deployment.

    Read from the file rather than transcribed, for `servers/pyexec`'s reason: two transcribed
    copies of a Deployment's CPU limit agreed with each other while the pod was resized under them.
    """
    manifest = yaml.safe_load(DEPLOYMENT.read_text(encoding="utf-8"))
    containers = manifest["spec"]["template"]["spec"]["containers"]
    limits = next(c for c in containers if c["name"] == "server")["resources"]["limits"]
    assert int(limits["cpu"]) == DEFAULT_MAX_CONCURRENT_PREDICTIONS, (
        f"the ceiling is {DEFAULT_MAX_CONCURRENT_PREDICTIONS} slots and the pod is limited to "
        f"{limits['cpu']} cores; a slot is a core, so the two have to be the same number"
    )


@pytest.mark.parametrize("value", ["0", "-1"])
def test_the_ceiling_set_to_nothing_refuses_at_import_and_names_the_variable(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """This server's ceiling has no "off", and it now says so instead of implying it.

    `MCP_MAX_SESSIONS=0` means "no ceiling" one layer down, so `0` is the value an operator is most
    likely to try here — and it means the opposite. `Admission` refuses it, but its message names
    the ceiling rather than the variable that set it, which leaves a CrashLoopBackOff and a number
    whose source has to be guessed. The assertion is on the variable's own name, because that is
    the one thing an operator reading a crash loop can act on.
    """
    monkeypatch.setenv("CHEMCLAW_RXNPREDICT_MAX_CONCURRENT_PREDICTIONS", value)
    with pytest.raises(ValueError, match="CHEMCLAW_RXNPREDICT_MAX_CONCURRENT_PREDICTIONS"):
        reimported(tools)
