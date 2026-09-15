"""The structural-size bound: a large molecule is refused before it can crash the canonicaliser.

`MolToSmiles` on a large linear molecule overflows the C stack (an uncatchable SIGSEGV), so the
bound is what stands between one ~20 KB authenticated call and the pod. These tests pin the two
independent limits and that the refusal message never echoes the offending megastring.
"""

from __future__ import annotations

import pytest
from mcp_server_kit import limits
from mcp_server_kit.limits import (
    MAX_MOLECULE_ATOMS,
    MAX_SMILES_CHARS,
    atom_count_error,
    smiles_length_error,
)


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
