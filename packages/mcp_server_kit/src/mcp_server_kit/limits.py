"""A structural-size bound every server applies before it canonicalises a SMILES.

**RDKit's `MolToSmiles` and the tautomer canonicalizer recurse over the molecular graph, and a
large enough linear molecule overflows the C stack** — the process dies with SIGSEGV (exit 139),
which no `try`/`except` in Python can catch. Measured: `MolToSmiles(MolFromSmiles("C" * 20000))`
segfaults, while the *parse* that produced the molecule returns normally in ~40 ms. So one ~20 KB
authenticated tool call takes the whole pod down, and with it every other session sharing it — a
denial of service that costs the caller one request.

The defence has to sit **before** any canonicalisation and it is the same in four servers
(`chem`, `safety`, `rxnlabel`, `rxnpredict`), so it lives here once. Two independent bounds, both
config-driven (a bound written as a magic number is one nobody can loosen for a real megamolecule
without editing code):

- **`MAX_SMILES_CHARS`** — a cheap guard applied to the raw string *before* it is parsed, so a
  pathological megastring never reaches `MolFromSmiles` at all. A real reagent SMILES is tens of
  characters; the default is far above anything a process chemist submits.
- **`MAX_MOLECULE_ATOMS`** — applied to `mol.GetNumAtoms()` after a successful parse and before
  canonicalisation. This is the bound that actually stops the segfault, because the recursion depth
  scales with the atom count, not the string length.

Neither function raises: they return a *worded reason* or `None`. A server that refuses (a chemist
is waiting) raises its own `ValueError` subclass with the reason; a server that ingests a corpus
leniently (`rxnlabel`) treats a reason as "could not be read" and drops the one species. The reason
string is caller-safe — it quotes only sizes, never the offending megastring — so it is safe to
surface to the model verbatim through `connector_app`.
"""

from __future__ import annotations

import os
import threading
from typing import NamedTuple

__all__ = [
    "MAX_MOLECULE_ATOMS",
    "MAX_SMILES_CHARS",
    "Admission",
    "Slots",
    "atom_count_error",
    "smiles_length_error",
]

MAX_SMILES_CHARS = int(os.environ.get("MCP_MAX_SMILES_CHARS", "4000"))
MAX_MOLECULE_ATOMS = int(os.environ.get("MCP_MAX_MOLECULE_ATOMS", "2000"))


def smiles_length_error(
    smiles: str,
    *,
    subject: str = "the structure given",
    max_chars: int = MAX_SMILES_CHARS,
) -> str | None:
    """A worded reason `smiles` is too long to parse safely, or `None` if it is within bounds.

    Applied to the raw string *before* `MolFromSmiles`, so a megastring never reaches the parser.
    The message quotes the length and the limit, never the string itself — a 500 KB SMILES echoed
    into a refusal would flood the log and the model context it is meant to protect.
    """
    if len(smiles) > max_chars:
        return (
            f"{subject} is {len(smiles)} characters, above the {max_chars}-character limit. A real "
            "reagent SMILES is far shorter; this bound stops a pathological string from exhausting "
            "the canonicaliser."
        )
    return None


def atom_count_error(
    num_atoms: int,
    *,
    subject: str = "the structure given",
    max_atoms: int = MAX_MOLECULE_ATOMS,
) -> str | None:
    """A worded reason a molecule of `num_atoms` atoms is too large, or `None` if within bounds.

    Applied after a successful parse and **before** any `MolToSmiles`/tautomer canonicalisation,
    which recurse over the graph and overflow the C stack (an uncatchable SIGSEGV) on a large
    linear molecule. `max_atoms` is far above any real reagent.
    """
    if num_atoms > max_atoms:
        return (
            f"{subject} has {num_atoms} atoms, above the {max_atoms}-atom limit. Canonicalising a "
            "molecule this large can overflow the underlying C library's stack and crash the "
            "server; no real reagent approaches this size."
        )
    return None


class Slots(NamedTuple):
    """The outcome of asking for concurrency slots, decided under the lock.

    `free` is carried out of the lock with the decision rather than read afterwards, because a
    refusal message quoting a count sampled later is quoting a number that was never true: between
    the refusal and the read, another call can finish.

    Attributes:
        charged: The slots actually taken, or `None` if the budget had no room. Not always the
            `cost` asked for — see `Admission.take`'s clamp.
        free: Slots free at the instant the decision was made.
    """

    charged: int | None
    free: int


class Admission:
    """A ceiling on how much of a server may run at once: the counter, the clamp and the lock.

    **This class does not raise, for the same reason the two bounds above do not.** A refusal has to
    be worded for the caller who receives it, and that wording is the one genuinely per-server part:
    `chem`'s names a replica because raising its ceiling cannot help, `calc`'s names a knob and
    leads with a marker Chemclaw3 matches, `rxnlabel`'s tells a drain to re-send the identical
    batch. So the arithmetic lives here once and the sentence stays with the server, exactly as
    `smiles_length_error` returns a reason and lets its caller raise.

    **What was measured before this was extracted.** Five servers carried a copy of this class, and
    with every string literal erased the mechanisms reduced to *three* distinct bodies: `chem` and
    `pyexec` byte-identical to each other, `rxnlabel` and `rxnpredict` byte-identical to each other,
    and `calc` differing from both in one expression — the exception it raises. With `cost`
    defaulting to 1 the first split is not a difference at all, so what five copies actually varied
    was the error type and the message. That is the case for extracting the rest.

    **Guarded by a lock rather than an `asyncio.Semaphore`**, and this is the part worth keeping in
    one place: slots are taken on the event loop and given back from whichever thread or callback
    finishes the work, and **nothing ever waits**. A full budget is an immediate refusal. A
    semaphore would make a caller queue, and a queued minute-long call returns after its
    `request_timeout` has expired — computed at the expense of one somebody is still waiting for.

    A server subclasses this and adds the verb its call sites use:

        class Admission(mcp_server_kit.limits.Admission):
            def acquire(self, what: str, cost: int = 1) -> int:
                taken = self.take(cost)
                if taken.charged is None:
                    raise MyError(f"... {taken.free} of {self.limit} ...")
                return taken.charged
    """

    #: The noun this server counts, used in the construction refusal. A subclass sets it, because
    #: "would refuse every depiction" and "would refuse every labelling batch" are the operator's
    #: own vocabulary — the same argument as the refusal message, at one word instead of a
    #: paragraph, and so not worth an `__init__` override.
    unit = "call"

    def __init__(self, limit: int) -> None:
        """Args: limit: the most slots that may be held at once. Must be at least one."""
        if limit < 1:
            raise ValueError(f"an admission ceiling of {limit} would refuse every {self.unit}")
        self._limit = limit
        self._lock = threading.Lock()
        self._in_flight = 0

    @property
    def limit(self) -> int:
        """The configured ceiling, in slots."""
        return self._limit

    @property
    def in_flight(self) -> int:
        """How many slots are held right now."""
        with self._lock:
            return self._in_flight

    def take(self, cost: int = 1) -> Slots:
        """Take `cost` slots if the budget has room, without waiting and without raising.

        Args:
            cost: How many slots this call occupies. **Clamped into `1..limit`**: a cost of zero
                would make a call uncounted, and a cost above the ceiling would make it permanently
                unadmittable, so an over-large one takes the pod exclusively instead. That clamp is
                the subtle half of this class and the reason it is worth having one copy of.

        Returns:
            `Slots`, whose `charged` is the slots taken — which `release` must be given back, and
            which is not always `cost` — or `None` when the budget had no room.
        """
        charge = max(1, min(cost, self._limit))
        with self._lock:
            free = self._limit - self._in_flight
            if charge > free:
                return Slots(charged=None, free=free)
            self._in_flight += charge
            return Slots(charged=charge, free=free - charge)

    def release(self, cost: int = 1) -> None:
        """Give `cost` slots back. Never below zero, so one double release cannot open the gate."""
        with self._lock:
            self._in_flight = max(0, self._in_flight - cost)
