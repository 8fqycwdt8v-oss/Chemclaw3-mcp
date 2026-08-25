"""The bounds a run happens inside, in one place, so nothing hard-codes one of them.

Every field here is a number that decides what a hostile or careless program can cost. They are
gathered into one frozen object rather than spread across `sandbox.py` and `runner.py` because the
two processes must agree about them: the parent serialises this into the payload, the child applies
it, and a value that existed in only one of those places would be a bound that silently did not
apply.

**Why the defaults are what they are.** An analysis in a conversation turn is seconds of arithmetic
over a table a tool just returned — not a simulation. So the wall clock is short enough that a stuck
run is noticed inside one turn, and the memory ceiling is generous enough that importing pandas and
RDKit does not itself trip it. A ceiling that a legitimate import trips is a ceiling that gets
raised without thought the first time somebody hits it, which is worse than a slightly loose one.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

__all__ = ["Limits"]


@dataclass(frozen=True, slots=True)
class Limits:
    """What one `run_python` call may spend, and how much of its output may come back."""

    wall_seconds: float = 20.0
    """Hard wall clock, enforced by the parent with `killpg`.

    The one bound that holds whatever the child does to itself: a program that blocks in an
    uninterruptible syscall, ignores signals, or spawns children still dies here, because the parent
    kills the whole process *group* rather than the one pid it launched.
    """

    cpu_seconds: int = 15
    """CPU seconds for the user's code, measured from after the libraries are warmed.

    `RLIMIT_CPU` is cumulative over the process's whole life, so the child adds this to what it has
    already spent importing numpy, pandas, scipy and RDKit — otherwise the budget would be mostly
    consumed before the first line of the caller's program ran, and would shrink every time a
    dependency got slower to import.
    """

    memory_bytes: int = 2 * 1024**3
    """`RLIMIT_AS` — address space, not resident set.

    2 GiB is roughly four times what the four scientific libraries need at rest. Address space is
    the bound Linux can enforce without cgroups, and it is coarse: a library that reserves a large
    lazy mapping counts against it while never touching the pages. That coarseness is why this is
    set generously and why the container's own memory limit remains the real ceiling.
    """

    file_bytes: int = 16 * 1024**2
    """`RLIMIT_FSIZE`. The sandbox has a writable temp directory and nothing else, and this bounds
    what can be put in it — so a program cannot fill the node's disk through the one door it has."""

    open_files: int = 128
    """`RLIMIT_NOFILE`. Descriptor exhaustion is a denial of service against the pod, not just the
    run."""

    process_headroom: int = 16
    """How many *more* tasks than already exist the child may create, via `RLIMIT_NPROC`.

    Expressed as headroom rather than an absolute because Linux counts `RLIMIT_NPROC` per real user
    id and against tasks, threads included — so an absolute number would either be too low to let
    the BLAS keep the threads it already made, or high enough to be no bound at all. The child reads
    its own task count after warming and adds this, which makes the limit mean "no fork bomb"
    without meaning "no threads".
    """

    stdout_chars: int = 10_000
    """Captured stdout is truncated to this many characters.

    A cap on *context*, not on disk. Whatever comes back is read by a model on the next turn and
    paid for on every turn after that until compaction reaches it, so a program that prints a
    100,000-row frame must not be able to spend the caller's context window. Truncation is reported
    rather than silent.
    """

    result_chars: int = 20_000
    """The JSON-encoded `result` is truncated to this many characters, for the same reason."""

    result_rows: int = 200
    """Rows kept when a DataFrame or Series is encoded. Same reason again; a frame is the realistic
    way a result gets large without anybody meaning it to."""

    def as_dict(self) -> dict[str, Any]:
        """The payload form. The child reconstructs a `Limits` from exactly this."""
        return asdict(self)
