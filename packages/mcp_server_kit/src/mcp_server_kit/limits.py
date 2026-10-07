"""Resource bounds shared by the fleet: structure size, environment-read bounds, echo, admission.

RDKit's `MolToSmiles` and tautomer canonicaliser recurse over the molecular graph, and a large
linear molecule overflows the C stack — an uncatchable SIGSEGV that takes the pod down. So before
any canonicalisation every server applies two config-driven bounds:

- `MAX_SMILES_CHARS` on the raw string, before parsing.
- `MAX_MOLECULE_ATOMS` on the parsed atom count — the bound that stops the crash, with a ceiling
  derived from this process's stack so it cannot be raised past what survives.

Those checks return a caller-safe worded reason (sizes only) or `None`; the caller raises. Every
bound read here is recorded and reported by `/healthz` via `effective_bounds()`, so a deployment
override is visible from the probe. `env_bound`/`env_ratio` raise at import, because an unusable
bound must stop the pod rather than refuse every request.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import resource
import threading
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, ClassVar, NamedTuple, TypeVar

from mcp_server_kit.metrics import ADMISSION_CEILING, ADMISSION_IN_FLIGHT, ADMISSION_REFUSED

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

__all__ = [
    "ATOMS_PER_KIB_OF_STACK",
    "MAX_ECHO_CHARS",
    "MAX_MOLECULE_ATOMS",
    "MAX_SMILES_CHARS",
    "Admission",
    "AtCapacityError",
    "Slots",
    "at_capacity_marker",
    "atom_count_error",
    "echo",
    "effective_bounds",
    "env_bound",
    "env_ratio",
    "report_bound",
    "report_settings",
    "smiles_length_error",
    "stack_safe_atom_ceiling",
]


# : Every bound this process has resolved, keyed by the environment variable that moves it.
# : Written by `report_bound`, read by `/healthz`.
_EFFECTIVE: dict[str, int | float | None] = {}
_EFFECTIVE_LOCK = threading.Lock()


def report_bound(name: str, value: int | float | None) -> None:
    """Record the value this process is running a bound at, for `/healthz` to report.

    `env_bound`, `env_ratio` and `report_settings` call this; any other bound reader must too
    (`test_every_bound_a_deployment_can_move_is_reported_on_the_probe`).

    Args:
        name: The environment variable that moves the bound.
        value: The value in use after defaults, floors and clamps; `None` means the bound is off.
    """
    with _EFFECTIVE_LOCK:
        _EFFECTIVE[name] = value


def report_settings(settings: Any) -> None:
    """Record every numeric field of a `pydantic-settings` object under its environment name.

    The name is `env_prefix` plus the upper-cased field name, or a literal `validation_alias`.
    `bool` fields are switches, not bounds, and are skipped. Duck-typed so the kit does not depend
    on
    `pydantic_settings`.
    """
    prefix = str(settings.model_config.get("env_prefix", ""))
    for field, info in type(settings).model_fields.items():
        value = getattr(settings, field)
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        alias = info.validation_alias
        name = alias if isinstance(alias, str) else f"{prefix}{field}".upper()
        report_bound(name, value)


def effective_bounds() -> dict[str, int | float | None]:
    """Every bound this process has resolved so far, sorted by name — what `/healthz` reports.

    Servers import their tool modules before `connector_app` runs, and the kit records its own
    bounds
    before the lifespan completes, so the set is complete by the first probe.
    """
    with _EFFECTIVE_LOCK:
        return dict(sorted(_EFFECTIVE.items()))


def _refused(name: str, value: float, default: float, minimum: float, consequence: str) -> str:
    """The sentence both readers raise when a value is under the floor its call site declared.

    Names the variable, the value set, the floor and why, and the default to return to.
    """
    return (
        f"{name}={value:g} is below the minimum of {minimum:g}: {consequence}. A bound has no "
        f"'off' setting, so unset {name} for the default of {default:g}, or give it a value of "
        f"at least {minimum:g}."
    )


def env_bound(
    name: str, *, default: int, minimum: int, consequence: str, maximum: int | None = None
) -> int:
    """One resource bound read from the environment at import, refused here if it cannot work.

    A bound below its floor (e.g. `0`) would start a pod that refuses every request, so it is a
    startup failure instead. A bound guarding a crash also takes a `maximum`. A non-integer is
    refused
    rather than ignored; an empty or whitespace value counts as unset. Raises rather than returning
    a
    reason because at import the only reader is an operator reading a container log.

    Args:
        name: The environment variable, named in every refusal.
        default: The value when the variable is unset, also named in the refusal.
        minimum: The smallest value that still lets the server work; declared by the call site.
        consequence: A clause completing "…: <consequence>." — what the rejected value would do.
        maximum: The largest value, where exceeding it breaks the server rather than loosening it;
            `None` for no ceiling.

    Returns:
        The configured value, within `minimum` and (if given) `maximum`.

    Raises:
        ValueError: The value is not a whole number, or is outside the bounds.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        report_bound(name, default)
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
        raise ValueError(_refused(name, value, default, minimum, consequence))
    if maximum is not None and value > maximum:
        raise ValueError(
            f"{name}={value} is above the maximum of {maximum}: {consequence}. This ceiling is not "
            f"a preference — above it the failure is a crash rather than a loosened bound — so "
            f"unset {name} for the default of {default}, or give it a value of at most {maximum}."
        )
    report_bound(name, value)
    return value


def env_ratio(name: str, *, default: float, minimum: float, consequence: str) -> float:
    """One dimensionless ratio read from the environment at import, refused if it cannot work.

    The `env_bound` rules for a float: a floor and no ceiling (raising a sanity ratio only loosens
    it), and a non-finite or non-numeric value is refused naming the variable.

    Args:
        name: The environment variable, named in every refusal.
        default: The ratio when the variable is unset, also named in the refusal.
        minimum: The smallest ratio that still lets the server work; declared by the call site.
        consequence: A clause completing "…: <consequence>." — what the rejected value would do.

    Returns:
        The configured ratio, which is at least `minimum`.

    Raises:
        ValueError: The value is not a finite number, or is below `minimum`.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        report_bound(name, default)
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(
            f"{name}={raw!r} is not a number, so this server cannot size the bound it controls; "
            f"unset it for the default of {default:g} or give it a value of at least {minimum:g}"
        ) from None
    # `float()` accepts "nan" and "inf"; both would slip past the floor and disable the bound.
    if not math.isfinite(value):
        raise ValueError(
            f"{name}={raw!r} is not a finite number, and a bound multiplied by it is never "
            f"crossed; unset it for the default of {default:g} or give it a finite value of at "
            f"least {minimum:g}"
        )
    if value < minimum:
        raise ValueError(_refused(name, value, default, minimum, consequence))
    report_bound(name, value)
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
# : Atoms of linear chain the canonicaliser survives per KiB of C stack, with a factor-of-two
# : margin over the measured ~2 atoms/KiB (linear in stack size); branched graphs recurse
# differently.
ATOMS_PER_KIB_OF_STACK = 1


def stack_safe_atom_ceiling(*, floor: int) -> int:
    """The largest `MAX_MOLECULE_ATOMS` this process's own C stack can survive.

    Derived from `RLIMIT_STACK`'s soft limit times `ATOMS_PER_KIB_OF_STACK`, since the stack size is
    a
    property of the container.

    Args:
        floor: The bound's default. The ceiling is never below it, so an unchanged deployment still
            starts; a derivation under it is logged at WARNING.

    Returns:
        The ceiling in atoms, at least `floor`; `floor` for an unlimited stack.
    """
    soft, _hard = resource.getrlimit(resource.RLIMIT_STACK)
    if soft == resource.RLIM_INFINITY:
        return floor
    derived = (soft // 1024) * ATOMS_PER_KIB_OF_STACK
    if derived < floor:
        logger.warning(
            "this process has a %d KiB stack, which the canonicaliser survives to about %d atoms, "
            "below MCP_MAX_MOLECULE_ATOMS' default of %d; the default is kept so that a deployment "
            "that changed nothing still starts, but a molecule near it may crash this pod",
            soft // 1024,
            derived,
            floor,
        )
        return floor
    return derived


#: The default, named so the ceiling derivation can floor at it rather than at a second literal.
DEFAULT_MAX_MOLECULE_ATOMS = 2000

MAX_MOLECULE_ATOMS = env_bound(
    "MCP_MAX_MOLECULE_ATOMS",
    default=DEFAULT_MAX_MOLECULE_ATOMS,
    # One atom, for the same reason: a parsed molecule always has at least one, so `0` refuses
    # every molecule that got past the parser — after the parse, which is the expensive half.
    minimum=1,
    # The one ceiling here: raising this bound past what the stack survives re-arms the SIGSEGV.
    maximum=stack_safe_atom_ceiling(floor=DEFAULT_MAX_MOLECULE_ATOMS),
    consequence=(
        "below it every molecule would be refused after it is parsed and before it is "
        "canonicalised; above it a large enough molecule overflows the C stack and kills this pod "
        "with an uncatchable SIGSEGV, taking every other session sharing it"
    ),
)


def smiles_length_error(
    smiles: str,
    *,
    subject: str = "the structure given",
    max_chars: int = MAX_SMILES_CHARS,
) -> str | None:
    """A worded reason `smiles` is too long to parse safely, or `None` if it is within bounds.

    Applied before `MolFromSmiles`; quotes only the length and limit, never the string.
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

    Applied after parsing and before any canonicalisation, which would overflow the C stack.
    """
    if num_atoms > max_atoms:
        return (
            f"{subject} has {num_atoms} atoms, above the {max_atoms}-atom limit. Canonicalising a "
            "molecule this large can overflow the underlying C library's stack and crash the "
            "server; no real reagent approaches this size."
        )
    return None


MAX_ECHO_CHARS = env_bound(
    "MCP_MAX_ECHO_CHARS",
    # Enough to recognise a structure by its head; `echo` appends the full length after it.
    default=120,
    # One character, because a refusal that quotes nothing of what it refused is one a chemist
    # cannot match to the call that produced it; at `0` every echo would be an ellipsis and a count.
    minimum=1,
    consequence="a refusal would quote none of the input it refused",
)


def echo(text: str, *, limit: int | None = None) -> str:
    """Caller-supplied text, bounded for quoting in a refusal: the head, then the full length.

    A `ValueError` reaches the model verbatim, so every refusal quoting caller text goes through
    this
    (`test_no_refusal_interpolates_caller_text_past_the_echo_bound`).

    Args:
        text: The caller's string, unstripped.
        limit: Characters kept before the ellipsis; `None` means `MAX_ECHO_CHARS`, read at call
        time.

    Returns:
        `text` if at most `limit` characters, else its head, an ellipsis and `(<length> chars)`.
    """
    bound = MAX_ECHO_CHARS if limit is None else limit
    return text if len(text) <= bound else f"{text[:bound]}… ({len(text)} chars)"


class Slots(NamedTuple):
    """The outcome of asking for concurrency slots, decided under the lock.

    `free` is captured with the decision, so a refusal quotes a count that was true.

    Attributes:
        charged: The slots taken (see `Admission.take`'s clamp), or `None` if there was no room.
        free: Slots free at the instant the decision was made.
    """

    charged: int | None
    free: int


def at_capacity_marker(server: str) -> str:
    """The token that opens every full-pod refusal from `server`: `[<server>-at-capacity]`.

    A refused MCP call carries only text, so this prefix is how a caller tells "full, retry shortly"
    from "bad input"; Chemclaw3 matches it to queue and retry.
    """
    return f"[{server}-at-capacity]"


class AtCapacityError(ValueError):
    """This pod is full; the identical call may well succeed once admitted work finishes.

    A `ValueError` so `connector_app` passes it verbatim; built by `Admission.refuse`.
    """


class Admission:
    """A ceiling on how much of a server may run at once: the counter, the clamp and the lock.

    Does not raise: the refusal wording is per-server, so a subclass adds the verb that raises.
    Guarded by a lock, not an `asyncio.Semaphore`, because nothing may wait: a full budget is an
    immediate refusal, since a queued long call would finish after its caller's timeout.

        class Admission(mcp_server_kit.limits.Admission):
            server = "mine"

            def acquire(self, what: str, cost: int = 1) -> int:
                taken = self.take(cost)
                if taken.charged is None:
                    raise self.refuse(f"... {taken.free} of {self.limit} ...")
                return taken.charged
    """

    # : The noun this server counts, for the construction refusal; a subclass sets it.
    unit = "call"

    #: The server this gate belongs to: the `server` label on the admission metrics and the name in
    #: its at-capacity marker. A subclass sets it once, as it sets `unit`.
    server: ClassVar[str] = ""

    def __init__(self, limit: int, *, server: str | None = None) -> None:
        """Create a gate of `limit` slots.

        Args:
            limit: The most slots that may be held at once; at least one.
            server: Overrides the class's `server`; one must be set, or occupancy is published under
            an
                empty label and the refusal marker matches nothing.
        """
        if limit < 1:
            raise ValueError(f"an admission ceiling of {limit} would refuse every {self.unit}")
        name = server or self.server
        if not name:
            raise ValueError("an admission gate must name its server")
        self._name = name
        self._limit = limit
        self._lock = threading.Lock()
        self._in_flight = 0
        ADMISSION_CEILING.labels(name).set(limit)
        ADMISSION_IN_FLIGHT.labels(name).set(0)

    @property
    def marker(self) -> str:
        """This gate's at-capacity token — see `at_capacity_marker`."""
        return at_capacity_marker(self._name)

    def refuse(self, sentence: str) -> AtCapacityError:
        """The full-pod refusal: the server's own sentence, led by the fleet's marker.

        Returned rather than raised, so the `raise` stays at the call site that owns the wording.
        """
        return AtCapacityError(f"{self.marker} {sentence}")

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
            cost: Slots this call occupies, clamped into `1..limit`: zero would go uncounted, and
            more
                than the ceiling would never be admitted, so it takes the pod exclusively instead.

        Returns:
            `Slots`, whose `charged` must be given back to `release`, or is `None` when there was no
            room.
        """
        charge = max(1, min(cost, self._limit))
        with self._lock:
            free = self._limit - self._in_flight
            if charge > free:
                ADMISSION_REFUSED.labels(self._name).inc()
                return Slots(charged=None, free=free)
            self._in_flight += charge
            ADMISSION_IN_FLIGHT.labels(self._name).set(self._in_flight)
            return Slots(charged=charge, free=free - charge)

    def release(self, cost: int = 1) -> None:
        """Give `cost` slots back. Never below zero, so one double release cannot open the gate."""
        with self._lock:
            self._in_flight = max(0, self._in_flight - cost)
            ADMISSION_IN_FLIGHT.labels(self._name).set(self._in_flight)

    async def hold(self, work: Awaitable[_T], charged: int) -> _T:
        """Await admitted work, giving its slots back when the *work* ends, not its awaiter.

        - `asyncio.shield`, because cancelling the awaiter does not stop the worker thread;
          releasing on
          cancellation would admit a retry beside the still-running original.
        - Release in a done-callback on the inner task, whether it returned, raised or was
          cancelled.
        - Retrieve the exception, so an abandoned failure does not log "never retrieved".

        Args:
            work: The admitted computation, not yet awaited.
            charged: The slots `take` charged for it (not always the cost asked for).

        Returns:
            Whatever the work returns; its exception, if it raises.
        """
        task = asyncio.ensure_future(work)

        def _release(done: asyncio.Future[_T]) -> None:
            self.release(charged)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(_release)
        return await asyncio.shield(task)

    async def admit(self, work: Awaitable[_T], acquire: Callable[[], int]) -> _T:
        """Charge admitted work and `hold` it, with the work built *before* anything is charged.

        Building the coroutine first means a bad call fails before a slot is taken, so no slot can
        leak
        between charge and release. On refusal the unscheduled coroutine is closed to avoid a
        warning.

        Args:
            work: The admitted computation, built but not yet awaited — see `hold`.
            acquire: The server's own charge, returning the slots taken or raising its refusal.

        Returns:
            Whatever the work returns; the refusal or the work's exception, if either raises.
        """
        try:
            charged = acquire()
        except BaseException:
            if isinstance(work, Coroutine):
                work.close()
            raise
        return await self.hold(work, charged)
