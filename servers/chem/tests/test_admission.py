"""How many depictions this pod accepts at once, and what it does with the call that arrives full.

`test_depiction_bound.py` is about the *arithmetic*: where `DEFAULT_MAX_CONCURRENT_RENDERS` comes
from, and how it relates to the probe budget and the pod's thread pool. It covers the ceiling only
indirectly — through the numbers, not through the gate — so the gate itself had no test at all in
the one server where it shipped first.

This file drives it. Four properties, each with its own failure:

- **The refusal is prompt.** A full pod turns a render away before laying out anything, rather than
  queueing it behind depictions that hold the interpreter. That is admission control and not the
  wall clock `CLAUDE.md` argues against: nothing is abandoned mid-burn, because nothing was started.
- **The slot outlives a caller that gave up.** Cancelling the awaiting coroutine does not stop the
  worker thread, so releasing on cancellation would hand the freed slot to a retry while the
  original layout was still holding the GIL.
- **Every other tool stays answerable.** A render is the only heavy thing here, and refusing a
  compound lookup because the pod is drawing would turn a CPU bound into an outage.
- **The gate and the number it enforces are the same object.** The ceiling is read once, at import,
  from `CHEMCLAW_CHEM_MAX_CONCURRENT_RENDERS`.

The gated set is checked against the *served* surface rather than a list kept here, for the reason
this repository keeps relearning: the thing that must not be forgotten is exactly the thing a
forgetful change adds. Note that it is **not** checked against the manifest's `state_changing` list
the way `servers/calc`'s is — `render_structure` is `read_only` there, correctly, because drawing a
molecule changes nothing. Cost and mutability are different axes, and this server is the one that
makes that obvious.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator

import pytest
from chemclaw_mcp_chem import tools
from chemclaw_mcp_chem.engine.admission import (
    ADMISSION_MARKER,
    DEFAULT_MAX_CONCURRENT_RENDERS,
    Admission,
)
from mcp_server_kit.testing import reimported

#: What the blocking stand-in holds a slot for, and the yardstick a refusal is measured against.
BLOCK_SECONDS = 2.0

#: What a refusal may cost as a fraction of the work it would have queued behind. A prompt refusal
#: is a lock and a raise; a queued one waits for a depiction to finish.
MAX_REFUSAL_FRACTION = 0.25


class _BlockingRender:
    """A stand-in for `render_svg` that holds its worker thread until it is told to stop.

    A real depiction would work and would make the timing assertions depend on how fast this runner
    draws. What is under test is the gate, not RDKit — `test_depiction_bound.py` measures RDKit.
    """

    def __init__(self) -> None:
        """Two events, and a peak counter so "refused before starting" is checkable."""
        self.started = threading.Event()
        self.finish = threading.Event()
        self.peak = 0
        self._live = 0
        self._lock = threading.Lock()

    def __call__(self, smiles: str, highlight_atoms: list[int] | None = None) -> str:
        """Record that a worker thread got here, then block until released."""
        with self._lock:
            self._live += 1
            self.peak = max(self.peak, self._live)
        self.started.set()
        self.finish.wait(30)
        with self._lock:
            self._live -= 1
        return "<svg/>"


async def _settle() -> None:
    """Let the shielded task's done-callback run, which is what gives a slot back."""
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.fixture
def one_slot(monkeypatch: pytest.MonkeyPatch) -> Iterator[Admission]:
    """Run the server's gate at a ceiling of one, so "full" is one render rather than eight."""
    gate = Admission(1)
    monkeypatch.setattr(tools, "_admission", gate)
    yield gate


def test_a_ceiling_below_one_is_refused_at_construction() -> None:
    """A ceiling of zero would refuse every depiction, which is a misconfiguration."""
    with pytest.raises(ValueError, match="would refuse every depiction"):
        Admission(0)


def test_the_gate_refuses_once_the_ceiling_is_reached_and_reopens_when_one_finishes() -> None:
    """The counter itself, with no transport and no event loop in the way."""
    gate = Admission(2)
    gate.acquire("a render_structure")
    gate.acquire("a render_structure")
    assert gate.in_flight == 2
    with pytest.raises(ValueError, match="already rendering 2 structures"):
        gate.acquire("a render_structure")
    gate.release()
    gate.acquire("a render_structure")
    assert gate.in_flight == 2


def test_a_double_release_cannot_open_the_gate() -> None:
    """One miscounted release must not create a slot that the ceiling never granted."""
    gate = Admission(1)
    gate.acquire("a render_structure")
    gate.release()
    gate.release()
    assert gate.in_flight == 0
    gate.acquire("a render_structure")
    with pytest.raises(ValueError, match="already rendering 1 structures"):
        gate.acquire("a render_structure")


async def test_a_full_pod_refuses_the_next_render_before_starting_it(
    one_slot: Admission, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The driven saturation probe: one render in flight, the next refused promptly.

    "Promptly" is measured against the work it would have queued behind rather than against a bare
    clock — the render in flight holds its slot until this test releases it, so a gate that queued
    could not answer the second caller at all. The peak concurrency is asserted beside it, because
    a refusal that still reached the worker thread would satisfy the timing and be the defect.
    """
    blocking = _BlockingRender()
    monkeypatch.setattr(tools, "render_svg", blocking)

    running = asyncio.ensure_future(tools.render_structure("CCO"))
    await asyncio.to_thread(blocking.started.wait, 30)
    assert one_slot.in_flight == 1

    started = time.perf_counter()
    with pytest.raises(ValueError, match="already rendering 1 structures"):
        await tools.render_structure("CCN")
    refusal = time.perf_counter() - started
    assert refusal < BLOCK_SECONDS * MAX_REFUSAL_FRACTION, (
        f"the refusal took {refusal * 1000:.1f} ms while a depiction held the only slot open "
        "indefinitely; a refusal that takes a measurable share of the work is a queue"
    )
    # The refusal names a *replica* rather than the knob, which is this server's own deviation
    # from `servers/calc`'s wording and is argued in `engine/admission.py`: RDKit holds the GIL
    # through a depiction, so raising the ceiling admits more renders onto the same serialised
    # interpreter and buys nothing. Advice a caller cannot act on is worse than none.
    assert "this server scales by replicas" in _refusal_text(one_slot)

    blocking.finish.set()
    await running
    await _settle()
    assert one_slot.in_flight == 0
    assert blocking.peak == 1, (
        f"{blocking.peak} depictions reached a worker thread against a ceiling of 1, so the "
        "refusal happened after the work started rather than before it"
    )


def _refusal_text(gate: Admission) -> str:
    """The message a full gate produces, so the assertion above reads it rather than restates it."""
    with pytest.raises(ValueError) as raised:
        gate.acquire("a render_structure")
    return str(raised.value)


async def test_the_slot_is_held_until_the_render_finishes_not_until_the_caller_gives_up(
    one_slot: Admission, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The half that breaks the retry loop.

    `Compute2DCoords` is superlinear and uninterruptible: cancelling the awaiting coroutine leaves
    the worker thread laying the molecule out, so releasing on cancellation would hand the freed
    slot to a retry while the interpreter was still held by the first one.
    """
    blocking = _BlockingRender()
    monkeypatch.setattr(tools, "render_svg", blocking)

    running = asyncio.ensure_future(tools.render_structure("CCO"))
    await asyncio.to_thread(blocking.started.wait, 30)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    await _settle()

    assert one_slot.in_flight == 1, (
        "the slot came back when the caller gave up, while the worker thread was still drawing"
    )
    with pytest.raises(ValueError, match="already rendering 1 structures"):
        await tools.render_structure("CCN")

    blocking.finish.set()
    for _ in range(300):
        await asyncio.sleep(0.01)
        if one_slot.in_flight == 0:
            break
    assert one_slot.in_flight == 0, "the slot never came back once the render finished"


async def test_every_other_tool_stays_answerable_while_the_pod_is_rendering(
    one_slot: Admission, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A render is the only heavy tool here; the rest must not be refused because of it."""
    blocking = _BlockingRender()
    monkeypatch.setattr(tools, "render_svg", blocking)

    running = asyncio.ensure_future(tools.render_structure("CCO"))
    await asyncio.to_thread(blocking.started.wait, 30)

    assert (await tools.resolve_compound("water")).smiles
    assert await tools.enumerate_torsions("CC(=O)Nc1ccccc1") is not None

    blocking.finish.set()
    await running
    await _settle()


#: The one gated tool, named here because it is the whole set — and asserted against the served
#: surface below rather than trusted, so a second heavy tool cannot arrive ungated.
GATED = {"render_structure"}


def test_only_the_depiction_is_gated_and_it_is_gated() -> None:
    """Derived from the served surface, so a heavy tool added next year is gated or this fails.

    Deliberately *not* derived from the manifest's `state_changing` list, which is how
    `servers/calc` checks the same thing: `render_structure` is `read_only` there and correctly so.
    Cost and mutability are different axes, and reusing one list for both would either gate the
    enumerations (which change nothing and cost nothing) or ungate the one tool that lays out a
    molecule and holds the interpreter while it does — milliseconds rather than microseconds, and
    the only tool here that holds it at all. No figure is transcribed: this sentence shipped saying
    97 ms, which is a molecule `MAX_DEPICTION_CHARS` refuses outright. What the ceiling is derived
    from is `test_depiction_bound.py`'s `WORST_LEGAL_MOLECULE`, measured there.
    """
    manager = tools.server._tool_manager
    served = {tool.name for tool in asyncio.run(tools.server.list_tools())}
    gated = {
        name
        for name in served
        if getattr(getattr(manager.get_tool(name), "fn", None), ADMISSION_MARKER, False)
    }
    assert gated == GATED, (
        f"ungated tools that this file expects to be gated: {sorted(GATED - gated)}; "
        f"newly gated tools: {sorted(gated - GATED)}"
    )


def test_the_ceiling_the_gate_was_built_from_is_the_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default is settable from outside the image, which is what puts it in the fleet ratchet.

    Read off the module under two environments rather than re-typed into this file. The version
    this replaces had a `_configured_ceiling()` helper *here* that restated `tools.py`'s own
    expression, so it compared the test to itself and never touched `tools._admission` — the same
    shape that left `servers/rxnlabel` able to hardcode its batch bound with 209 tests green.
    `tests/test_fleet.py` is what then refuses a shipped file that moves the variable.
    """
    monkeypatch.delenv("CHEMCLAW_CHEM_MAX_CONCURRENT_RENDERS", raising=False)
    assert reimported(tools)._admission.limit == DEFAULT_MAX_CONCURRENT_RENDERS
    monkeypatch.setenv("CHEMCLAW_CHEM_MAX_CONCURRENT_RENDERS", "3")
    assert reimported(tools)._admission.limit == 3


def test_the_shipped_gate_enforces_the_shipped_default() -> None:
    """The module-level gate is the one the server actually serves behind, at the default ceiling.

    Cheap and easy to leave out, and it is what makes every `monkeypatch`ed ceiling above evidence
    about the real gate rather than about an `Admission` the tests built for themselves.
    """
    assert isinstance(tools._admission, Admission)
    assert tools._admission.limit == DEFAULT_MAX_CONCURRENT_RENDERS


def test_a_gated_tool_still_advertises_its_real_signature() -> None:
    """`functools.wraps` is load-bearing: without it the tool's schema is `(*args, **kwargs)`.

    FastMCP builds each tool's input schema from `inspect.signature`, which follows `__wrapped__`.
    A gate that quietly replaced every argument name with `kwargs` would be invisible in this
    server's own tests and fatal to the agent reading the schema.
    """
    schema = asyncio.run(tools.server.list_tools())
    rendered = next(tool for tool in schema if tool.name == "render_structure")
    assert set(rendered.inputSchema["properties"]) == {"smiles", "highlight_atoms"}
