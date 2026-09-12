"""How much this pod labels at once, and what it does with the batch that arrives when full.

`MAX_BATCH` bounds one request; this bounds how many are in flight. The fleet's rule says in so
many words that those are not the same bound, and here the distinction has teeth: the caller is
Chemclaw3's corpus drain, so "many maximal batches arriving together" is normal traffic rather than
an attack.

Four properties, each with its own failure:

- **The refusal is prompt.** A full pod turns a batch away before any work starts. A queued batch
  of 500 comes back after `connector.yaml`'s `request_timeout` has expired — an answer nobody is
  waiting for, computed at the expense of one somebody is.
- **The slot outlives a caller that gave up.** Releasing on cancellation would hand the freed slot
  to the drain's retry while the original batch was still burning a core.
- **`labeller_version` stays answerable.** It is how a caller decides whether it needs to label at
  all, so refusing it under load creates work rather than shedding it.
- **A slot is a core, not a call.** The RDKit path is measured GIL-bound at one core; the mapper is
  a transformer whose intra-op width torch takes from the node rather than from the cgroup, so a
  call-counting ceiling under-counts by whatever that happens to be.

The gated set is checked against the *served* surface rather than a list kept here, for the reason
this repository keeps relearning: the thing that must not be forgotten is exactly the thing a
forgetful change adds.
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
from chemclaw_mcp_rxnlabel import tools
from chemclaw_mcp_rxnlabel.engine import mapping
from chemclaw_mcp_rxnlabel.engine.admission import (
    ADMISSION_MARKER,
    DEFAULT_MAX_BATCH,
    DEFAULT_MAX_CONCURRENT_BATCHES,
    Admission,
)
from mcp_server_kit.testing import reimported

DEPLOYMENT = Path(__file__).resolve().parents[1] / "deploy" / "deployment.yaml"

#: One reaction, five species — the shape the drain sends, and what `_represent` is driven over.
REACTION = "CC(=O)Cl.Nc1ccccc1>CCN(CC)CC.ClCCl>CC(=O)Nc1ccccc1"

#: How long the blocking stand-in holds a slot. Long enough that a *queued* refusal would have to
#: wait most of it, short enough that the file stays quick.
BLOCK_SECONDS = 2.0

#: What a refusal may cost as a fraction of the work it is queued behind. A prompt refusal is a
#: lock and a raise; a queued one waits for the batch in flight.
MAX_REFUSAL_FRACTION = 0.25


def _batch(count: int) -> list[tools.ReactionRequest]:
    """`count` identical representation requests."""
    return [
        tools.ReactionRequest(
            id=str(index), reaction_smiles=REACTION, species=["CC(=O)Cl", "Nc1ccccc1"]
        )
        for index in range(count)
    ]


class _BlockingBatch:
    """A stand-in for `_represent` that holds its worker thread until it is told to stop.

    A real batch would work too and would make the timing assertions depend on how fast this runner
    happens to be. What is under test is the gate, not RDKit.
    """

    def __init__(self) -> None:
        """Two events: one the test waits on, one it sets to let the work finish."""
        self.started = threading.Event()
        self.finish = threading.Event()
        self.peak = 0
        self._live = 0
        self._lock = threading.Lock()

    def __call__(self, reactions: list[Any]) -> list[Any]:
        """Record that a worker thread got here, then block until released."""
        with self._lock:
            self._live += 1
            self.peak = max(self.peak, self._live)
        self.started.set()
        self.finish.wait(30)
        with self._lock:
            self._live -= 1
        return []


async def _settle() -> None:
    """Let the shielded task's done-callback run, which is what gives a slot back."""
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.fixture
def one_slot(monkeypatch: pytest.MonkeyPatch) -> Iterator[Admission]:
    """Run the server's gate at a ceiling of one, so "full" is one batch rather than two."""
    gate = Admission(1)
    monkeypatch.setattr(tools, "_admission", gate)
    yield gate


def test_a_ceiling_below_one_is_refused_at_construction() -> None:
    """A ceiling of zero would refuse every batch, which is a misconfiguration, not a setting."""
    with pytest.raises(ValueError, match="would refuse every batch"):
        Admission(0)


def test_the_gate_refuses_once_the_ceiling_is_reached_and_reopens_when_one_finishes() -> None:
    """The counter itself, with no transport and no event loop in the way."""
    gate = Admission(2)
    assert gate.acquire("a represent_reactions") == 1
    assert gate.acquire("a name_reactions") == 1
    with pytest.raises(ValueError, match="0 of its 2 labelling slots free"):
        gate.acquire("a represent_reactions")
    gate.release()
    assert gate.acquire("a represent_reactions") == 1


def test_a_cost_wider_than_the_pod_is_clamped_rather_than_made_unadmittable() -> None:
    """A batch that costs more than the whole ceiling runs alone; it is never refused forever.

    The alternative is a tool that configuration has deleted: a pod with one slot and a mapper
    told to use four threads would refuse every labelling call, permanently, with nothing in the
    message to say the ceiling was the reason.
    """
    gate = Admission(2)
    assert gate.acquire("a represent_reactions", 64) == 2
    with pytest.raises(ValueError, match="0 of its 2 labelling slots free"):
        gate.acquire("a name_reactions")
    gate.release(2)
    assert gate.in_flight == 0


async def test_a_full_pod_refuses_the_next_batch_before_starting_it(
    one_slot: Admission, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The driven saturation probe: one batch in flight, the next refused promptly.

    "Promptly" is measured against the work it would have queued behind rather than against a bare
    clock: the batch in flight holds its slot for `BLOCK_SECONDS`, so a gate that queued would
    answer the second caller at the end of that and a gate that refuses answers immediately. The
    peak concurrency is asserted too, because a refusal that still started the work would satisfy
    the timing and be the defect.
    """
    blocking = _BlockingBatch()
    monkeypatch.setattr(tools, "_represent", blocking)

    running = asyncio.ensure_future(tools.represent_reactions(_batch(3)))
    await asyncio.to_thread(blocking.started.wait, 30)
    assert one_slot.in_flight == 1

    started = time.perf_counter()
    with pytest.raises(ValueError, match="0 of its 1 labelling slots free"):
        await tools.name_reactions([tools.NamingRequest(id="1", reaction_smiles=REACTION)])
    refusal = time.perf_counter() - started
    assert refusal < BLOCK_SECONDS * MAX_REFUSAL_FRACTION, (
        f"the refusal took {refusal * 1000:.1f} ms while a batch held the only slot for "
        f"{BLOCK_SECONDS} s; a refusal that takes a measurable share of the work it was queued "
        "behind is a queue"
    )

    blocking.finish.set()
    await running
    await _settle()
    assert one_slot.in_flight == 0
    assert blocking.peak == 1, (
        f"{blocking.peak} batches reached a worker thread against a ceiling of 1, so the refusal "
        "happened after the work started rather than before it"
    )


async def test_the_slot_is_held_until_the_work_finishes_not_until_the_caller_gives_up(
    one_slot: Admission, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The half that breaks the retry loop.

    Cancelling the awaiting coroutine does not stop the worker thread, so releasing on cancellation
    would hand the freed slot to the drain's retry while the original batch was still burning a
    core — the pod would believe it had room it does not have.
    """
    blocking = _BlockingBatch()
    monkeypatch.setattr(tools, "_represent", blocking)

    running = asyncio.ensure_future(tools.represent_reactions(_batch(3)))
    await asyncio.to_thread(blocking.started.wait, 30)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    await _settle()

    assert one_slot.in_flight == 1, (
        "the slot came back when the caller gave up, while the worker thread was still labelling"
    )
    with pytest.raises(ValueError, match="0 of its 1 labelling slots free"):
        await tools.name_reactions([tools.NamingRequest(id="1", reaction_smiles=REACTION)])

    blocking.finish.set()
    for _ in range(300):
        await asyncio.sleep(0.01)
        if one_slot.in_flight == 0:
            break
    assert one_slot.in_flight == 0, "the slot never came back once the work finished"


async def test_labeller_version_stays_answerable_while_the_pod_is_full(
    one_slot: Admission, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is how a caller decides whether it needs to label at all; refusing it adds work.

    The same argument `servers/calc` makes for `calculation_key`: a probe a client uses to *avoid*
    paying for work must stay answerable when the pod cannot afford any more of it.
    """
    blocking = _BlockingBatch()
    monkeypatch.setattr(tools, "_represent", blocking)

    running = asyncio.ensure_future(tools.represent_reactions(_batch(3)))
    await asyncio.to_thread(blocking.started.wait, 30)

    answered = await tools.labeller_version()
    assert answered.version

    blocking.finish.set()
    await running
    await _settle()


def test_a_batch_is_charged_the_mappers_threads_rather_than_one_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slot is a core, and the mapper is the reason this server cannot count calls.

    RXNMapper is a transformer: torch releases the GIL and parallelises inside one forward pass, at
    a width it takes from the machine's physical cores rather than from the container's cgroup —
    and no image in this fleet pins `OMP_NUM_THREADS`. So a gate counting calls would admit two
    batches on a two-core pod while each ran four, eight or sixty-four threads. Charged its
    threads, one mapped batch fills the pod and the next call is refused rather than admitted onto
    a machine with no core left for it.

    Driven with a stub `torch` rather than the real extra, which no test environment here carries:
    the number under test is what `inference_threads()` *reads*, and reading it from a real torch
    would assert this runner's core count instead.
    """
    assert mapping.inference_threads() == 1, "no mapper is installed, so the cost must be one core"

    monkeypatch.setattr(mapping, "_MAPPER", object())
    monkeypatch.setattr(mapping, "_TRIED", True)
    stub = types.ModuleType("torch")
    stub.get_num_threads = lambda: 4  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "torch", stub)

    assert mapping.inference_threads() == 4
    assert tools._batch_slots() == 4

    gate = Admission(4)
    assert gate.acquire("a represent_reactions", tools._batch_slots()) == 4
    with pytest.raises(ValueError, match="0 of its 4 labelling slots free"):
        gate.acquire("a name_reactions", 1)


#: The one served tool that is deliberately ungated, and the argument is `calculation_key`'s: a
#: caller asks it whether a stored label is stale *before* deciding to pay for labelling, so
#: refusing it under load pushes work onto a pod that is already full.
UNGATED = {"labeller_version"}


def test_every_labelling_tool_is_gated_and_only_the_version_probe_is_not() -> None:
    """Derived from the served surface, so a tool added next year is gated or this fails.

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
        f"ungated labelling tools: {sorted(served - UNGATED - gated)}; "
        f"gated probes that should stay answerable: {sorted(gated & UNGATED)}"
    )


def test_both_bounds_are_environment_variables_and_not_constants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ceiling and the batch bound are both settable from outside the image.

    `MAX_BATCH` was a bare `500` in the source, which is a bound nobody can loosen for a genuinely
    larger drain without editing code — and one the fleet's own ratchet over what a deployment may
    move could not see either.

    **This test claimed to read the module twice and did neither.** It set two variables and then
    compared a re-typed copy of the expression under test — `int(os.environ.get(...))` — to the
    numbers it had just set, which asserts that `os.environ.get` works. `tools.MAX_BATCH` was never
    read: hardcoding it back to `500` left this and 208 other tests green. It now executes the
    module's own source under each environment and reads the values off *that*.
    """
    monkeypatch.setenv("CHEMCLAW_RXNLABEL_MAX_BATCH", "7")
    monkeypatch.setenv("CHEMCLAW_RXNLABEL_MAX_CONCURRENT_BATCHES", "9")
    configured = reimported(tools)
    assert (configured.MAX_BATCH, configured._admission.limit) == (7, 9)
    monkeypatch.delenv("CHEMCLAW_RXNLABEL_MAX_BATCH")
    monkeypatch.delenv("CHEMCLAW_RXNLABEL_MAX_CONCURRENT_BATCHES")
    default = reimported(tools)
    assert (default.MAX_BATCH, default._admission.limit) == (
        DEFAULT_MAX_BATCH,
        DEFAULT_MAX_CONCURRENT_BATCHES,
    )


def test_the_shipped_gate_enforces_the_shipped_default() -> None:
    """The module-level gate is the one the server serves behind, at the default ceiling.

    Cheap and easy to leave out, and it is what makes every `monkeypatch`ed ceiling in this file
    evidence about the real gate rather than about an `Admission` the tests built for themselves.
    `servers/chem` has had it since its gate was written; this server copied the gate and not the
    test.
    """
    assert isinstance(tools._admission, Admission)
    assert tools._admission.limit == DEFAULT_MAX_CONCURRENT_BATCHES
    assert tools.MAX_BATCH == DEFAULT_MAX_BATCH


def test_a_gated_tool_still_advertises_its_real_signature() -> None:
    """`functools.wraps` is load-bearing: without it the tool's schema is `(*args, **kwargs)`.

    FastMCP builds each tool's input schema from `inspect.signature`, which follows `__wrapped__`.
    A gate that quietly replaced every argument name with `kwargs` would be invisible in this
    server's own tests and fatal to the agent reading the schema.
    """
    schema = asyncio.run(tools.server.list_tools())
    batched = next(tool for tool in schema if tool.name == "represent_reactions")
    assert set(batched.inputSchema["properties"]) == {"reactions"}


def test_the_ceiling_is_the_pods_own_core_count() -> None:
    """A slot is a core, so the default has to equal `limits.cpu` in the shipped Deployment.

    Read from the file rather than transcribed. `servers/pyexec` shipped exactly this coupling as
    two transcribed copies, and lowering `limits.cpu` there would have put two runs on one core
    with the suite green; `servers/chem/tests/test_depiction_bound.py` is where the pattern of
    reading the Deployment comes from.
    """
    manifest = yaml.safe_load(DEPLOYMENT.read_text(encoding="utf-8"))
    containers = manifest["spec"]["template"]["spec"]["containers"]
    limits = next(c for c in containers if c["name"] == "server")["resources"]["limits"]
    assert int(limits["cpu"]) == DEFAULT_MAX_CONCURRENT_BATCHES, (
        f"the ceiling is {DEFAULT_MAX_CONCURRENT_BATCHES} slots and the pod is limited to "
        f"{limits['cpu']} cores; a slot is a core, so the two have to be the same number"
    )


@pytest.mark.parametrize("value", ["0", "-1"])
@pytest.mark.parametrize(
    "variable", ["CHEMCLAW_RXNLABEL_MAX_BATCH", "CHEMCLAW_RXNLABEL_MAX_CONCURRENT_BATCHES"]
)
def test_a_bound_set_to_nothing_refuses_at_import_and_names_the_variable(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    """Neither of this server's bounds has an "off", and both now say so instead of implying it.

    `MCP_MAX_SESSIONS=0` means "no ceiling" one layer down, so `0` is the value an operator is most
    likely to try here — and it meant the opposite in both knobs. The ceiling raised a `ValueError`
    that named the *number* and not the variable, which is a CrashLoopBackOff and a source to
    guess. `CHEMCLAW_RXNLABEL_MAX_BATCH=0` was quieter and worse: measured, the pod started, passed
    its readiness probe, and refused every batch with "0 reactions in one request exceeds the batch
    limit of 0".

    The assertion is on the variable's own name appearing in the message, because that is the one
    thing an operator reading a crash loop can act on.
    """
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError, match=variable):
        reimported(tools)
