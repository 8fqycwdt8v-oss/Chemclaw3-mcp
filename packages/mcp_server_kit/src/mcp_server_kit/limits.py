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

Neither of those two functions raises: they return a *worded reason* or `None`. A server that
refuses (a chemist is waiting) raises its own `ValueError` subclass with the reason; a server that
ingests a corpus leniently (`rxnlabel`) treats a reason as "could not be read" and drops the one
species. The reason string is caller-safe — it quotes only sizes, never the offending megastring —
so it is safe to surface to the model verbatim through `connector_app`.

**`env_bound` is the exception to that rule and its docstring says why**: it is how every server in
this fleet reads a resource bound out of the environment at import, and a bound that cannot work
has to stop the process rather than hand somebody a reason there is nobody to receive. It is here
rather than beside any one server for the reason `Admission` is — every server already imports this
module, and what varied between the hand-written copies was the sentence, not the check.
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
    "env_bound",
    "smiles_length_error",
]


def env_bound(name: str, *, default: int, minimum: int, consequence: str) -> int:
    """One resource bound read from the environment at import, refused here if it cannot work.

    **The defect this exists for is a pod that starts and then refuses every request.** A bare
    `int(os.environ.get(name, default))` accepts `0` and every negative, and a bound whose whole job
    is to refuse has no "off": measured on `rxnlabel` before its own guard,
    `CHEMCLAW_RXNLABEL_MAX_BATCH=0` started the pod, passed its readiness probe, and answered every
    call with "0 reactions in one request exceeds the batch limit of 0". `0` is also the value an
    operator is most likely to try, because `MCP_MAX_SESSIONS=0` means *no ceiling* one layer down
    and here it means the opposite. So an unusable bound is a **startup** failure, which a kubelet
    reports as a pod that never became ready rather than as a server quietly serving nothing.

    **Raising is the whole point, and it is why this does not follow `Admission` and the two size
    bounds in this module** — both of which return a worded reason and let their caller raise
    (`D-2026-09-15-five-copies-varied-the-message-not-the-mechanism` has that argument). Those run
    inside a request, where the reason is written for a chemist waiting on an answer and the caller
    chooses the exception type the model will read. This runs at import: there is no request, no
    model and no caller, and the only reader is an operator looking at a container log. A returned
    reason would have exactly one possible handler at every call site, which is the shape the Rule
    of Three says to inline rather than abstract.

    **What stays per-site is the wording and the floor**, which is the half that genuinely varies.
    `consequence` is the server's own sentence about what the value would break, and `minimum` is
    not `1` everywhere: `CHEMCLAW_CHEM_RENDER_SIZE_PX` is a canvas in pixels, not a count of
    things, and a one-pixel canvas is not a smaller picture.

    A non-integer is refused the same way rather than falling back with a warning, which is what
    `executor.thread_pool_size` and `sessions.max_sessions` do with theirs. Those two have a
    defensible runtime default and are read per call; these are read once, before the server has
    accepted anything, and silently ignoring a number a deployment deliberately set is how a
    deployment comes to believe a ceiling it does not have. An empty or whitespace-only value *is*
    treated as unset, matching both of those and the way a Kubernetes `env:` entry with no value
    arrives.

    Args:
        name: The environment variable, named in every refusal because it is the one thing an
            operator reading a crash loop can act on — a traceback out of `int()` names neither
            the variable nor the value.
        default: What this bound is when the variable is unset. Named in the refusal too, so the
            way back is in the message rather than in this repository.
        minimum: The smallest value that still leaves the server able to do its work. Declared by
            the call site, because only the call site knows what the number measures.
        consequence: A clause completing "…: <consequence>." — what the rejected value would do to
            this server, in the operator's own vocabulary.

    Returns:
        The configured value, which is at least `minimum`.

    Raises:
        ValueError: The variable is set to something that is not a whole number, or to a number
            below `minimum`.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(
            f"{name}={raw!r} is not a whole number, so this server cannot size the bound it "
            f"controls; unset it for the default of {default} or give it an integer of at least "
            f"{minimum}"
        ) from None
    if value < minimum:
        raise ValueError(
            f"{name}={value} is below the minimum of {minimum}: {consequence}. A bound has no "
            f"'off' setting, so unset {name} for the default of {default}, or give it a value of "
            f"at least {minimum}."
        )
    return value


MAX_SMILES_CHARS = env_bound(
    "MCP_MAX_SMILES_CHARS",
    # Far above anything a process chemist submits; a real reagent SMILES is tens of characters.
    default=4000,
    # One character, because the guard refuses anything *longer* than this: at `0` every structure
    # in the fleet is refused before it is parsed, including a one-atom `C`.
    minimum=1,
    consequence="every structure this fleet is given would be refused before it is parsed",
)
MAX_MOLECULE_ATOMS = env_bound(
    "MCP_MAX_MOLECULE_ATOMS",
    default=2000,
    # One atom, for the same reason: a parsed molecule always has at least one, so `0` refuses
    # every molecule that got past the parser — after the parse, which is the expensive half.
    minimum=1,
    consequence="every molecule would be refused after it is parsed and before it is canonicalised",
)


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
