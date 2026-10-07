"""How much this pod predicts at once, and what it does with a call that arrives when full.

One tool call is not one thread: the consensus tools fan out over every enabled predictor, so a
ceiling counting calls would under-count by the enabled-model list. Properties pinned: a call is
charged its fan-out; a full pod refuses promptly instead of queueing past `request_timeout`; a
slot outlives a caller that gave up; `list_available_models` and `classify_reaction` (which
offload nothing) stay answerable. The gated set is derived from the served surface.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

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
    """One ensemble call puts one worker thread per enabled predictor in flight at once.

    Six doubles each hold a thread for `BLOCK_SECONDS`: serial would take six times that, and one
    thread would peak at one. This is the measurement the cost model rests on.
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
    """An ensemble holds the pod, and the next call is refused promptly.

    The in-flight forward pass is held open until the test releases it, so a gate that queued could
    not answer at all.
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
    """A cost above the ceiling runs alone; it is never made unadmittable.

    At the shipped ceiling an ensemble over five predictors is exactly this case, by intent.
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
    """A prediction is charged the model's torch threads rather than one call.

    Torch's intra-op width comes from `OMP_NUM_THREADS` or the node's cores, not the cgroup; the
    image pins it to 1, a deployment may raise it, and the charge follows what torch reports. A stub
    `torch` is used, so the test reads what `inference_threads()` reads, not this runner's cores.
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
    """Every predicting tool is gated except the two that offload nothing, derived from the surface.

    A hand-kept list would agree with itself while a new tool shipped as an uncounted way to load
    the pod.
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
    """The ceiling is settable from the environment, read off the module under two environments.

    Re-typing the expression here would only prove `os.environ.get` works.
    """
    monkeypatch.delenv("CHEMCLAW_RXNPREDICT_MAX_CONCURRENT_PREDICTIONS", raising=False)
    assert reimported(tools)._admission.limit == DEFAULT_MAX_CONCURRENT_PREDICTIONS
    monkeypatch.setenv("CHEMCLAW_RXNPREDICT_MAX_CONCURRENT_PREDICTIONS", "11")
    assert reimported(tools)._admission.limit == 11


def test_the_shipped_gate_enforces_the_shipped_default() -> None:
    """The module-level gate is the one the server serves behind, at the default ceiling.

    This makes every monkeypatched ceiling in this file evidence about the real gate rather than
    about an `Admission` the tests built for themselves.
    """
    assert isinstance(tools._admission, Admission)
    assert tools._admission.limit == DEFAULT_MAX_CONCURRENT_PREDICTIONS


def test_a_gated_tool_still_advertises_its_real_signature() -> None:
    """`functools.wraps` is load-bearing: without it the tool's schema is `(*args, **kwargs)`.

    FastMCP builds the input schema from `inspect.signature`, which follows `__wrapped__`.
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
    """The charge does not follow the caller's `models` argument.

    The charge reads the deployment's enabled list, because a cost a caller could lower by naming
    one model is a ceiling it could walk past. Execution narrows on `models` and the charge does
    not; both halves are asserted, since a version narrowing neither would pass on the charge alone.
    """
    registry_of(*(_SlowPredictor(f"slow_{index}") for index in range(FAN_OUT)))
    one = ["slow_0"]
    # `_forward_predictors` is annotated `list[object]` in the server; the objects it hands back
    # are the `_SlowPredictor`s `registry_of` just registered, and their names are the subject.
    narrowed = cast(list[BaseForwardPredictor], tools._forward_predictors(one))
    assert [predictor.name for predictor in narrowed] == one, (
        "the execution path no longer narrows on `models`, so there is nothing to walk past"
    )
    ensemble = tools._forward_ensemble_slots()
    gate = _RecordingAdmission(ensemble * 4)
    monkeypatch.setattr(tools, "_admission", gate)

    # By keyword first (how FastMCP invokes the tool in service) and positionally second, so a
    # charge reading the narrowing off either calling convention is caught.
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
    """The ceiling has no "off": `0` or a negative refuses at import, naming the variable.

    `MCP_MAX_SESSIONS=0` means "no ceiling" one layer down, so `0` is what an operator tries here.
    The variable's name in the message is what an operator reading a crash loop can act on.
    """
    monkeypatch.setenv("CHEMCLAW_RXNPREDICT_MAX_CONCURRENT_PREDICTIONS", value)
    with pytest.raises(ValueError, match="CHEMCLAW_RXNPREDICT_MAX_CONCURRENT_PREDICTIONS"):
        reimported(tools)
