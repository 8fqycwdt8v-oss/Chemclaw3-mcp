"""The structural-size bound: a large molecule is refused before it can crash the canonicaliser.

`MolToSmiles` on a large linear molecule overflows the C stack (an uncatchable SIGSEGV). These
tests pin the two independent limits and that the refusal never echoes the offending string.
"""

from __future__ import annotations

import logging
import resource
from collections.abc import Iterator

import pytest
from mcp_server_kit import limits
from mcp_server_kit.limits import (
    MAX_ECHO_CHARS,
    MAX_MOLECULE_ATOMS,
    MAX_SMILES_CHARS,
    atom_count_error,
    echo,
    smiles_length_error,
)
from mcp_server_kit.testing import reimported


def test_a_short_string_is_within_bounds() -> None:
    """A real reagent SMILES is far under the limit and returns no reason."""
    assert smiles_length_error("CCO") is None


def test_an_over_length_string_is_refused() -> None:
    """A string past `MAX_SMILES_CHARS` is refused before it is ever parsed."""
    reason = smiles_length_error("C" * (MAX_SMILES_CHARS + 1))
    assert reason is not None
    assert str(MAX_SMILES_CHARS) in reason


def test_the_refusal_never_echoes_the_megastring() -> None:
    """The message quotes lengths, not the input — echoing 500 KB would defeat its own purpose."""
    payload = "N" * 500_000
    reason = smiles_length_error(payload)
    assert reason is not None
    assert payload not in reason


def test_a_small_molecule_is_within_the_atom_bound() -> None:
    """A molecule under `MAX_MOLECULE_ATOMS` returns no reason."""
    assert atom_count_error(3) is None


def test_an_over_large_molecule_is_refused() -> None:
    """The atom bound is the one that actually stops the segfault (recursion scales with atoms)."""
    reason = atom_count_error(MAX_MOLECULE_ATOMS + 1)
    assert reason is not None
    assert str(MAX_MOLECULE_ATOMS) in reason


def test_the_subject_is_named_in_the_message() -> None:
    """A refusal names what was rejected so a caller can act on it."""
    reason = smiles_length_error("C" * (MAX_SMILES_CHARS + 1), subject="component 4 of 9")
    assert reason is not None and "component 4 of 9" in reason


# ---------------------------------------------------------------------------------------------
# The concurrency ceiling. Five servers carried a copy of this class before it moved here; what
# follows holds the parts that were subtle enough to be worth having one of.


def test_a_cost_above_the_ceiling_takes_the_pod_exclusively_rather_than_being_unadmittable() -> (
    None
):
    """A call costing more than the whole budget is admitted alone rather than refused forever.

    Unclamped, it could never fit at any ceiling; clamping to `limit` makes it take the pod
    exclusively.
    """
    budget = limits.Admission(4, server="test")
    taken = budget.take(cost=99)
    assert taken.charged == 4
    assert budget.in_flight == 4
    assert budget.take().charged is None, "the pod is now exclusively held"


def test_a_cost_of_zero_is_charged_one_so_nothing_runs_uncounted() -> None:
    """The clamp's lower half. A zero cost would make a tool invisible to its own ceiling."""
    budget = limits.Admission(2, server="test")
    assert budget.take(cost=0).charged == 1
    assert budget.in_flight == 1


def test_the_free_count_is_taken_under_the_lock_with_the_decision() -> None:
    """The free count in a refusal is read under the lock with the decision.

    Read afterwards, another call may have finished and the message would quote a capacity that did
    not exist when the request was refused.
    """
    budget = limits.Admission(3, server="test")
    budget.take(cost=2)
    refused = budget.take(cost=2)
    assert refused.charged is None
    assert refused.free == 1, "one slot free at the instant of the refusal"


def test_a_double_release_cannot_open_the_gate() -> None:
    """The floor at zero. Without it, releasing more than was taken mints slots out of nothing."""
    budget = limits.Admission(2, server="test")
    budget.take()
    budget.release()
    budget.release()
    budget.release()
    assert budget.in_flight == 0
    assert budget.take().charged == 1
    assert budget.take().charged == 1
    assert budget.take().charged is None, "still exactly two slots, not five"


def test_a_ceiling_below_one_is_refused_at_construction_naming_the_server_s_own_noun() -> None:
    """`unit` is the one word each server keeps, so the operator reads their own vocabulary."""
    with pytest.raises(ValueError, match="would refuse every call"):
        limits.Admission(0, server="test")

    class Renders(limits.Admission):
        unit = "depiction"
        server = "test"

    with pytest.raises(ValueError, match="would refuse every depiction"):
        Renders(0)


def test_nothing_ever_waits() -> None:
    """A full budget refuses immediately rather than blocking, which is why it is a lock.

    A queued minute-long call would return after its `request_timeout` expired. Asserted by taking
    the whole budget on one thread and timing a refusal on another.
    """
    import threading
    import time

    budget = limits.Admission(1, server="test")
    assert budget.take().charged == 1

    elapsed: list[float] = []

    def ask() -> None:
        start = time.monotonic()
        assert budget.take().charged is None
        elapsed.append(time.monotonic() - start)

    thread = threading.Thread(target=ask)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive(), "a full budget must refuse, never block"
    assert elapsed and elapsed[0] < 0.1, elapsed


def test_concurrent_takers_never_exceed_the_ceiling() -> None:
    """The lock's actual job, driven rather than asserted about.

    Twenty threads racing for four slots must grant exactly four. Without the lock the
    read-modify-write around `_in_flight` interleaves and the budget over-grants.
    """
    import threading

    budget = limits.Admission(4, server="test")
    granted: list[int] = []
    lock = threading.Lock()
    start = threading.Barrier(20)

    def ask() -> None:
        start.wait()
        taken = budget.take()
        if taken.charged is not None:
            with lock:
                granted.append(taken.charged)

    threads = [threading.Thread(target=ask) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert sum(granted) == 4, granted
    assert budget.in_flight == 4


def test_hold_releases_the_charge_when_the_work_ends_not_when_its_awaiter_is_cancelled() -> None:
    """The shield is the point: a cancelled caller must not free a slot the work still holds.

    A thread-backed job is started, its awaiter cancelled mid-flight, and the budget read while the
    worker is still running — it must still be charged — then again after the worker finishes.
    """
    import asyncio
    import threading

    budget = limits.Admission(4, server="test")
    gate = threading.Event()

    async def scenario() -> tuple[int, int]:
        taken = budget.take(3)
        assert taken.charged == 3
        waiter = asyncio.ensure_future(budget.hold(asyncio.to_thread(gate.wait, 5), 3))
        await asyncio.sleep(0.05)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        during = budget.in_flight
        gate.set()
        for _ in range(200):
            if budget.in_flight == 0:
                break
            await asyncio.sleep(0.01)
        return during, budget.in_flight

    during, after = asyncio.run(scenario())
    assert during == 3, "the slots went back while the worker thread was still running"
    assert after == 0


def test_hold_returns_the_result_and_releases_on_failure_without_an_unretrieved_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A result passes through; a raise passes through too and still gives the slot back."""
    import asyncio

    budget = limits.Admission(1, server="test")

    async def answer() -> int:
        return 42

    async def boom() -> int:
        raise ValueError("refused")

    async def scenario() -> int:
        assert budget.take().charged == 1
        value = await budget.hold(answer(), 1)
        assert budget.in_flight == 0
        assert budget.take().charged == 1
        with pytest.raises(ValueError, match="refused"):
            await budget.hold(boom(), 1)
        return value

    with caplog.at_level(logging.ERROR, logger="asyncio"):
        assert asyncio.run(scenario()) == 42
    assert budget.in_flight == 0
    assert "never retrieved" not in caplog.text


def test_admit_charges_after_the_work_is_built_so_a_malformed_call_costs_nothing() -> None:
    """A call its signature refuses fails before any slot is charged.

    The coroutine is built at the call site before `admit` runs, so a binding `TypeError` cannot
    leak a slot.
    """
    import asyncio

    budget = limits.Admission(1, server="test")

    async def work(x: int) -> int:
        return x

    def charge() -> int:
        taken = budget.take()
        assert taken.charged is not None
        return taken.charged

    async def scenario() -> int:
        with pytest.raises(TypeError):
            await budget.admit(work(1, 2), charge)  # type: ignore[call-arg]
        assert budget.in_flight == 0, "a call that never started kept its slot"
        return await budget.admit(work(7), charge)

    assert asyncio.run(scenario()) == 7
    assert budget.in_flight == 0


def test_a_refused_admission_closes_the_work_it_was_handed() -> None:
    """A refusal leaves nothing charged and the coroutine closed, so nothing warns it was dropped.

    Read off the coroutine's state, since the "never awaited" warning fires in a finaliser at an
    unpredictable time.
    """
    import asyncio
    import inspect

    budget = limits.Admission(1, server="test")

    async def work() -> None:
        raise AssertionError("refused work ran anyway")

    def refuse() -> int:
        raise ValueError("full")

    pending = work()

    async def scenario() -> None:
        with pytest.raises(ValueError, match="full"):
            await budget.admit(pending, refuse)

    asyncio.run(scenario())
    assert budget.in_flight == 0
    assert inspect.getcoroutinestate(pending) == inspect.CORO_CLOSED


def test_an_unset_bound_is_its_default() -> None:
    """The ordinary case: nothing in the environment, so the call site's own number stands."""
    assert limits.env_bound("MCP_A_BOUND_NOBODY_SETS", default=7, minimum=1, consequence="x") == 7


def test_an_empty_value_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Kubernetes `env:` entry with no value arrives as `""`, and it is not a bound of zero.

    Treated as unset, consistent with the kit's other env readers.
    """
    monkeypatch.setenv("MCP_A_BLANK_BOUND", "   \n\t ")
    assert limits.env_bound("MCP_A_BLANK_BOUND", default=7, minimum=1, consequence="x") == 7


def test_a_configured_bound_is_the_configured_number(monkeypatch: pytest.MonkeyPatch) -> None:
    """Surrounding whitespace is tolerated, because a YAML value rarely arrives trimmed."""
    monkeypatch.setenv("MCP_A_SET_BOUND", " 99 ")
    assert limits.env_bound("MCP_A_SET_BOUND", default=7, minimum=1, consequence="x") == 99


@pytest.mark.parametrize("value", ["0", "-1", "-2000"])
def test_a_bound_below_its_floor_names_the_variable_the_value_and_the_floor(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """The refusal names the variable, the value and the floor.

    An operator reading a crash loop needs all three to act.
    """
    monkeypatch.setenv("MCP_A_FLOORED_BOUND", value)
    with pytest.raises(ValueError) as refusal:
        limits.env_bound(
            "MCP_A_FLOORED_BOUND", default=7, minimum=4, consequence="nothing would be served"
        )
    message = str(refusal.value)
    assert "MCP_A_FLOORED_BOUND" in message
    assert value.lstrip("-") in message
    assert "minimum of 4" in message
    assert "default of 7" in message
    assert "nothing would be served" in message


def test_the_floor_is_the_call_sites_own_number(monkeypatch: pytest.MonkeyPatch) -> None:
    """A floor above 1 is a real case, not a hypothetical — `chem`'s render size is a canvas.

    Both directions on one site, so "refuses everything" cannot pass as "has a floor".
    """
    monkeypatch.setenv("MCP_A_PIXEL_BOUND", "5")
    with pytest.raises(ValueError, match="minimum of 6"):
        limits.env_bound("MCP_A_PIXEL_BOUND", default=320, minimum=6, consequence="x")
    monkeypatch.setenv("MCP_A_PIXEL_BOUND", "6")
    assert limits.env_bound("MCP_A_PIXEL_BOUND", default=320, minimum=6, consequence="x") == 6


def test_a_value_that_is_not_a_number_is_refused_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-numeric value is refused by name rather than defaulted.

    It is read once at startup; silently ignoring a configured number would leave a deployment
    believing in a ceiling it does not have.
    """
    monkeypatch.setenv("MCP_A_WORDY_BOUND", "four")
    with pytest.raises(ValueError) as refusal:
        limits.env_bound("MCP_A_WORDY_BOUND", default=7, minimum=1, consequence="x")
    message = str(refusal.value)
    assert "MCP_A_WORDY_BOUND" in message
    assert "four" in message
    assert "default of 7" in message


def test_this_modules_own_two_bounds_refuse_at_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """`limits.py`'s own two bounds go through `env_bound` and refuse at import.

    Module-scope order is invisible at the call site, so the module source is re-executed with each
    variable set blank.
    """
    for variable in ("MCP_MAX_SMILES_CHARS", "MCP_MAX_MOLECULE_ATOMS"):
        monkeypatch.setenv(variable, "0")
        with pytest.raises(ValueError, match=variable):
            reimported(limits)
        monkeypatch.delenv(variable)


def test_a_bound_with_no_ceiling_accepts_anything_above_its_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`maximum` is optional and the usual case is not to pass it.

    The complement of the two below: a bound whose job is taste has a floor and no ceiling, and
    adding the parameter must not have given every call site one by accident.
    """
    monkeypatch.setenv("MCP_A_TASTE_BOUND", "999999999")
    assert limits.env_bound("MCP_A_TASTE_BOUND", default=7, minimum=1, consequence="x") == 999999999


def test_a_bound_above_its_ceiling_is_refused_naming_the_variable_the_value_and_the_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bound above its ceiling is refused naming the variable, the value and the ceiling.

    Raising a crash guard far enough disables it; the message carries the way back.
    """
    monkeypatch.setenv("MCP_A_CRASH_BOUND", "5000")
    with pytest.raises(ValueError) as refusal:
        limits.env_bound(
            "MCP_A_CRASH_BOUND", default=100, minimum=1, maximum=4096, consequence="the pod dies"
        )
    message = str(refusal.value)
    assert "MCP_A_CRASH_BOUND" in message
    assert "5000" in message
    assert "maximum of 4096" in message
    assert "the pod dies" in message
    monkeypatch.setenv("MCP_A_CRASH_BOUND", "4096")
    assert (
        limits.env_bound(
            "MCP_A_CRASH_BOUND", default=100, minimum=1, maximum=4096, consequence="the pod dies"
        )
        == 4096
    )


def test_the_atom_bound_cannot_be_raised_past_what_the_stack_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`MAX_MOLECULE_ATOMS` cannot be raised past what the stack survives.

    The bound stops an uncatchable SIGSEGV, so without a ceiling the knob is also an off switch. The
    refusal names the derived ceiling, a property of the container's stack.
    """
    ceiling = limits.stack_safe_atom_ceiling(floor=limits.DEFAULT_MAX_MOLECULE_ATOMS)
    monkeypatch.setenv("MCP_MAX_MOLECULE_ATOMS", "999999999")
    with pytest.raises(ValueError) as refusal:
        reimported(limits)
    assert str(ceiling) in str(refusal.value)
    monkeypatch.setenv("MCP_MAX_MOLECULE_ATOMS", str(ceiling))
    assert ceiling == reimported(limits).MAX_MOLECULE_ATOMS


def test_the_ceiling_is_derived_from_this_process_s_own_stack_not_transcribed() -> None:
    """The atom ceiling is derived from this process's own stack, not transcribed.

    The crash threshold is linear in `ulimit -s`; `ATOMS_PER_KIB_OF_STACK` is that slope halved, so
    the derivation tracks the container it runs in.
    """
    soft, _hard = resource.getrlimit(resource.RLIMIT_STACK)
    floor = limits.DEFAULT_MAX_MOLECULE_ATOMS
    ceiling = limits.stack_safe_atom_ceiling(floor=floor)
    assert ceiling >= floor, "a deployment that changed nothing must still start"
    if soft != resource.RLIM_INFINITY:
        assert ceiling <= max(floor, (soft // 1024) * limits.ATOMS_PER_KIB_OF_STACK)


def test_a_stack_too_small_for_the_default_keeps_it_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stack too small for the default keeps the default and warns.

    A deployment that set nothing must not newly fail to start, so the derivation never goes below
    the default, but the thin margin is logged at WARNING.
    """
    with caplog.at_level(logging.WARNING, logger=limits.__name__):
        assert limits.stack_safe_atom_ceiling(floor=10**9) == 10**9
    assert any("may crash this pod" in record.getMessage() for record in caplog.records)


def test_a_ratio_is_read_with_the_same_discipline_as_a_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ratio is read with the same discipline as a count.

    All three arms: a value under the floor, a non-number, and the floor itself accepted.
    """
    monkeypatch.setenv("MCP_A_RATIO", "0")
    with pytest.raises(ValueError) as refusal:
        limits.env_ratio("MCP_A_RATIO", default=1.8, minimum=1.01, consequence="nothing answers")
    message = str(refusal.value)
    assert "MCP_A_RATIO" in message
    assert "1.01" in message and "1.8" in message
    assert "nothing answers" in message

    monkeypatch.setenv("MCP_A_RATIO", "loose")
    with pytest.raises(ValueError, match="is not a number"):
        limits.env_ratio("MCP_A_RATIO", default=1.8, minimum=1.01, consequence="x")

    monkeypatch.setenv("MCP_A_RATIO", "1.01")
    assert limits.env_ratio(
        "MCP_A_RATIO", default=1.8, minimum=1.01, consequence="x"
    ) == pytest.approx(1.01)


def test_a_ratio_has_a_floor_and_no_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ratio has a floor and no ceiling.

    Unlike the atom bound, raising a sanity ratio re-arms no crash, and a deployment must be able to
    loosen it without editing code.
    """
    monkeypatch.setenv("MCP_A_RATIO", "99")
    assert limits.env_ratio(
        "MCP_A_RATIO", default=1.8, minimum=1.01, consequence="x"
    ) == pytest.approx(99.0)


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "infinity"])
def test_a_ratio_that_is_not_finite_is_refused(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """`float()` parses these, and `nan < minimum` is False, so the floor alone would let them through.

    A NaN or infinite ratio makes every comparison against it false, which disables the bound.
    """
    monkeypatch.setenv("MCP_A_RATIO", raw)
    with pytest.raises(ValueError, match=r"MCP_A_RATIO.*not a finite number"):
        limits.env_ratio("MCP_A_RATIO", default=1.8, minimum=1.01, consequence="x")


def test_both_readers_refuse_a_low_value_in_the_same_words() -> None:
    """One sentence, because an operator meeting either needs the same four facts in the same order.

    `_refused` is what the two share; the parsing is what they do not. Compared as a *shape* rather
    than by asserting one literal twice, so that improving the wording moves both or neither.
    """
    count = limits._refused("A_COUNT", 0, 500, 1, "no batch is admitted")
    ratio = limits._refused("A_RATIO", 0, 1.8, 1.01, "nothing answers")
    for message, name, value, default, minimum in (
        (count, "A_COUNT", "0", "500", "1"),
        (ratio, "A_RATIO", "0", "1.8", "1.01"),
    ):
        assert message.startswith(f"{name}={value} is below the minimum of {minimum}:"), message
        assert f"unset {name} for the default of {default}" in message
        assert "has no 'off' setting" in message
    assert "500.0" not in count, "a count must not read as a float"


def test_an_echo_within_the_bound_is_the_text_itself() -> None:
    """A short structure is quoted whole, so the refusal still names what the chemist typed."""
    assert echo("CCO junk") == "CCO junk"
    exact = "C" * MAX_ECHO_CHARS
    assert echo(exact) == exact


def test_an_echo_past_the_bound_keeps_the_head_and_names_the_length() -> None:
    """The measured case: `"C" * 1500` is inside both structural bounds and was quoted whole."""
    payload = "C" * 1500
    shown = echo(payload)
    assert "C" * (MAX_ECHO_CHARS + 1) not in shown
    assert shown.startswith("C" * MAX_ECHO_CHARS)
    assert shown.endswith("(1500 chars)")
    assert len(shown) <= MAX_ECHO_CHARS + len("… (1500 chars)")


def test_the_echo_bound_is_the_environment_s(monkeypatch: pytest.MonkeyPatch) -> None:
    """One config-driven constant, read at import like every other bound in this module."""
    monkeypatch.setenv("MCP_MAX_ECHO_CHARS", "8")
    rebuilt = reimported(limits)
    assert rebuilt.MAX_ECHO_CHARS == 8
    assert rebuilt.echo("ABCDEFGHIJ") == "ABCDEFGH… (10 chars)"
    monkeypatch.setenv("MCP_MAX_ECHO_CHARS", "0")
    with pytest.raises(ValueError, match="MCP_MAX_ECHO_CHARS"):
        reimported(limits)


@pytest.fixture
def isolated_bounds() -> Iterator[None]:
    """The process-wide bound record, restored after the test that writes to it."""
    saved = dict(limits._EFFECTIVE)
    yield
    limits._EFFECTIVE.clear()
    limits._EFFECTIVE.update(saved)


def test_every_bound_the_two_readers_return_is_recorded(
    monkeypatch: pytest.MonkeyPatch, isolated_bounds: None
) -> None:
    """What `env_bound` and `env_ratio` return is what `/healthz` reports — default or override.

    Recorded on both paths: "the ceiling is the default" and "nothing reported a ceiling" are
    different facts.
    """
    monkeypatch.delenv("CHEMCLAW_PROBE_UNSET", raising=False)
    monkeypatch.setenv("CHEMCLAW_PROBE_SET", "12")
    monkeypatch.setenv("CHEMCLAW_PROBE_RATIO", "2.5")
    limits.env_bound("CHEMCLAW_PROBE_UNSET", default=4, minimum=1, consequence="none")
    limits.env_bound("CHEMCLAW_PROBE_SET", default=4, minimum=1, consequence="none")
    limits.env_ratio("CHEMCLAW_PROBE_RATIO", default=1.8, minimum=1.0, consequence="none")
    recorded = limits.effective_bounds()
    assert recorded["CHEMCLAW_PROBE_UNSET"] == 4
    assert recorded["CHEMCLAW_PROBE_SET"] == 12
    assert recorded["CHEMCLAW_PROBE_RATIO"] == 2.5
    # A refused value is not recorded: the process does not start with it.
    monkeypatch.setenv("CHEMCLAW_PROBE_REFUSED", "0")
    with pytest.raises(ValueError):
        limits.env_bound("CHEMCLAW_PROBE_REFUSED", default=4, minimum=1, consequence="none")
    assert "CHEMCLAW_PROBE_REFUSED" not in limits.effective_bounds()


def test_a_settings_object_is_recorded_under_the_names_its_environment_reads(
    monkeypatch: pytest.MonkeyPatch, isolated_bounds: None
) -> None:
    """A pydantic-settings object is recorded under the env names it reads.

    The name is derived as that library does (prefix plus field, upper-cased, or a literal
    `validation_alias`); a `bool` is a switch, not a bound, and is left out.
    """
    from pydantic import Field
    from pydantic_settings import BaseSettings, SettingsConfigDict

    class ProbeSettings(BaseSettings):
        model_config = SettingsConfigDict(env_prefix="CHEMCLAW_PROBE_")

        max_things: int = 5
        ratio: float = 1.5
        enabled: bool = True
        label: str = "x"
        aliased: int = Field(default=3, validation_alias="PROBE_ALIASED")

    monkeypatch.setenv("CHEMCLAW_PROBE_MAX_THINGS", "11")
    limits.report_settings(ProbeSettings())
    recorded = limits.effective_bounds()
    assert recorded["CHEMCLAW_PROBE_MAX_THINGS"] == 11
    assert recorded["CHEMCLAW_PROBE_RATIO"] == 1.5
    assert recorded["PROBE_ALIASED"] == 3
    assert "CHEMCLAW_PROBE_ENABLED" not in recorded
    assert "CHEMCLAW_PROBE_LABEL" not in recorded


def test_an_unnamed_gate_is_refused_at_construction() -> None:
    """A gate with no server would publish under an empty label and mint a marker nobody matches."""
    with pytest.raises(ValueError, match="must name its server"):
        limits.Admission(1)


def test_a_full_pod_refusal_leads_with_the_fleet_marker() -> None:
    """The head of the message is the only channel MCP gives a refusal, so the marker goes first.

    `calc`'s pre-existing token is this format's value for `"calc"`, which is what keeps every
    caller already matching it unchanged.
    """
    budget = limits.Admission(1, server="rxnpredict")
    refusal = budget.refuse("this pod is full")
    assert isinstance(refusal, ValueError)
    assert isinstance(refusal, limits.AtCapacityError)
    assert str(refusal) == "[rxnpredict-at-capacity] this pod is full"
    assert limits.at_capacity_marker("calc") == "[calc-at-capacity]"


def test_the_admission_gauges_follow_the_gate() -> None:
    """Occupancy is what an autoscaler reads here, so the gauges must move with every take/release.

    Driven through the real registry rather than the gate's own counter: the property an autoscaler
    depends on is what `/metrics` says, not what the object believes.
    """
    from prometheus_client import REGISTRY

    def sample(name: str) -> float | None:
        return REGISTRY.get_sample_value(name, {"server": "gauge-probe"})

    budget = limits.Admission(3, server="gauge-probe")
    refused_before = sample("chemclaw_mcp_admission_refused_total") or 0.0
    assert sample("chemclaw_mcp_admission_ceiling") == 3
    assert sample("chemclaw_mcp_admission_in_flight") == 0

    charged = budget.take(2).charged
    assert charged == 2
    assert sample("chemclaw_mcp_admission_in_flight") == 2

    assert budget.take(2).charged is None
    assert sample("chemclaw_mcp_admission_refused_total") == refused_before + 1
    assert sample("chemclaw_mcp_admission_in_flight") == 2

    budget.release(charged)
    assert sample("chemclaw_mcp_admission_in_flight") == 0
