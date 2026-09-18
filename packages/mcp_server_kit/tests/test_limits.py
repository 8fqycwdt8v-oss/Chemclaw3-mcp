"""The structural-size bound: a large molecule is refused before it can crash the canonicaliser.

`MolToSmiles` on a large linear molecule overflows the C stack (an uncatchable SIGSEGV), so the
bound is what stands between one ~20 KB authenticated call and the pod. These tests pin the two
independent limits and that the refusal message never echoes the offending megastring.
"""

from __future__ import annotations

import logging
import resource

import pytest
from mcp_server_kit import limits
from mcp_server_kit.limits import (
    MAX_MOLECULE_ATOMS,
    MAX_SMILES_CHARS,
    atom_count_error,
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
    """The clamp's upper half, and the reason it is not a refusal.

    A call whose cost exceeds the whole budget can never fit, so an unclamped comparison would
    refuse it at every ceiling and on every pod — permanently, with no configuration that helps.
    Clamping to `limit` admits it alone instead, which is the honest reading of "this one call is
    the pod's whole capacity".
    """
    budget = limits.Admission(4)
    taken = budget.take(cost=99)
    assert taken.charged == 4
    assert budget.in_flight == 4
    assert budget.take().charged is None, "the pod is now exclusively held"


def test_a_cost_of_zero_is_charged_one_so_nothing_runs_uncounted() -> None:
    """The clamp's lower half. A zero cost would make a tool invisible to its own ceiling."""
    budget = limits.Admission(2)
    assert budget.take(cost=0).charged == 1
    assert budget.in_flight == 1


def test_the_free_count_is_taken_under_the_lock_with_the_decision() -> None:
    """A refusal quoting a count read afterwards is quoting a number that was never true.

    `Slots` carries `free` out with the verdict for that reason: between a refusal and a later
    read, another call can finish, and the message would name a capacity that existed only after
    the request it is explaining was turned away.
    """
    budget = limits.Admission(3)
    budget.take(cost=2)
    refused = budget.take(cost=2)
    assert refused.charged is None
    assert refused.free == 1, "one slot free at the instant of the refusal"


def test_a_double_release_cannot_open_the_gate() -> None:
    """The floor at zero. Without it, releasing more than was taken mints slots out of nothing."""
    budget = limits.Admission(2)
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
        limits.Admission(0)

    class Renders(limits.Admission):
        unit = "depiction"

    with pytest.raises(ValueError, match="would refuse every depiction"):
        Renders(0)


def test_nothing_ever_waits() -> None:
    """A full budget refuses immediately rather than blocking, which is why it is a lock.

    An `asyncio.Semaphore` would queue, and a queued minute-long call returns after its
    `request_timeout` has expired — computed at the expense of one somebody is still waiting for.
    Asserted by taking the whole budget on one thread and timing a refusal on another.
    """
    import threading
    import time

    budget = limits.Admission(1)
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

    budget = limits.Admission(4)
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


def test_an_unset_bound_is_its_default() -> None:
    """The ordinary case: nothing in the environment, so the call site's own number stands."""
    assert limits.env_bound("MCP_A_BOUND_NOBODY_SETS", default=7, minimum=1, consequence="x") == 7


def test_an_empty_value_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Kubernetes `env:` entry with no value arrives as `""`, and it is not a bound of zero.

    Deliberate, and the same reading `executor.thread_pool_size` and `sessions.max_sessions` give
    theirs. The alternative — `int("")` — is a `ValueError` naming neither the variable nor the
    fact that the value was blank.
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
    """All three, because that is what an operator reading a container log has to act on.

    The message is the whole control here. `Admission` already refused a ceiling of `0` and said
    "an admission ceiling of 0 would refuse every depiction" — true, and it leaves somebody staring
    at a CrashLoopBackOff with a number whose source they have to guess.
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
    """`int("four")` raises a `ValueError` naming neither the variable nor what to do about it.

    Refused rather than defaulted-with-a-warning, which is what the two per-call reads in this kit
    do: those have a defensible runtime default, while this is read once before the server has
    accepted anything, and quietly ignoring a number a deployment set is how a deployment comes to
    believe in a ceiling it does not have.
    """
    monkeypatch.setenv("MCP_A_WORDY_BOUND", "four")
    with pytest.raises(ValueError) as refusal:
        limits.env_bound("MCP_A_WORDY_BOUND", default=7, minimum=1, consequence="x")
    message = str(refusal.value)
    assert "MCP_A_WORDY_BOUND" in message
    assert "four" in message
    assert "default of 7" in message


def test_this_modules_own_two_bounds_refuse_at_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bootstrap case: `limits.py` defines the helper *and* is one of its callers.

    `env_bound` has to be defined above `MAX_SMILES_CHARS` and `MAX_MOLECULE_ATOMS` for that to
    work at all, and a reader cannot tell from the call sites that it is — module-scope order is
    invisible at the point of use. Re-executing this module's own source with each variable set to
    nothing is what shows the order holds.
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
    """The half `minimum` could not reach: turning a crash guard *up* until it guards nothing.

    An operator reading a crash loop has the variable, what they set, and the largest value that
    works — the same three things the floor's refusal gives them, because the way back has to be in
    the message rather than in this repository.
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
    """The defect this ceiling exists for, driven through this module's own import.

    `MAX_MOLECULE_ATOMS` is the bound that stops an uncatchable SIGSEGV — the module docstring is
    written about exactly that — and before this it had a floor and no ceiling, so the knob provided
    to tune it was also an off switch for it. Measured on this container at the time it was added:
    `MCP_MAX_MOLECULE_ATOMS=999999999` was accepted at import and canonicalising `"C" * 20000` then
    killed the process with exit 139.

    The refusal has to name the derived ceiling rather than a transcribed number, because the
    ceiling is a property of the container's stack — see `stack_safe_atom_ceiling`.
    """
    ceiling = limits.stack_safe_atom_ceiling(floor=limits.DEFAULT_MAX_MOLECULE_ATOMS)
    monkeypatch.setenv("MCP_MAX_MOLECULE_ATOMS", "999999999")
    with pytest.raises(ValueError) as refusal:
        reimported(limits)
    assert str(ceiling) in str(refusal.value)
    monkeypatch.setenv("MCP_MAX_MOLECULE_ATOMS", str(ceiling))
    assert ceiling == reimported(limits).MAX_MOLECULE_ATOMS


def test_the_ceiling_is_derived_from_this_process_s_own_stack_not_transcribed() -> None:
    """A number in this file would be a claim about somebody else's `ulimit -s`.

    The threshold is linear in the stack across an eightfold range — measured by canonicalising
    `"C" * n` under a reduced `ulimit -s` and reading the exit status: a 1 MiB stack survives 2,000
    atoms and dies at 2,500, a 2 MiB stack dies at 4,500, an 8 MiB stack survives 18,000 and dies at
    20,000. `ATOMS_PER_KIB_OF_STACK` is that slope halved, so the derivation tracks the container it
    runs in rather than the one this was written on.
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
    """Floored rather than refused, and reported at WARNING rather than clamped in silence.

    A deployment that set nothing must not newly fail to start, so the derivation never returns
    below the module's own default. But a container whose stack cannot carry that default is one
    whose default is genuinely thin, and the only honest thing to do with that is say it where an
    operator reading the container log will meet it. The same choice Chemclaw3 makes when its
    compaction trigger floors.
    """
    with caplog.at_level(logging.WARNING, logger=limits.__name__):
        assert limits.stack_safe_atom_ceiling(floor=10**9) == 10**9
    assert any("may crash this pod" in record.getMessage() for record in caplog.records)
