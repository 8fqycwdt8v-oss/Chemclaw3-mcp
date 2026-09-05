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

import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

__all__ = ["Limits", "container_memory_limit", "default_memory_bytes"]

logger = logging.getLogger(__name__)

# Where a container's own memory limit is written, newest first. cgroup v2 is what OpenShift 4.13+
# and any RHEL 9 node uses; v1 is still what a Docker-on-older-kernel dev box gives, and this
# repository's own sandbox is one — so both are read rather than the one that happens to be here.
_CGROUP_V2_MAX = Path("/sys/fs/cgroup/memory.max")
_CGROUP_V1_MAX = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")

# What the server process itself needs beside the runs: the interpreter, FastMCP, the live MCP
# sessions and the request bodies. Measured at 58 MiB of RSS for this server's own imports; 256 MiB
# is that with room for the sessions and the payloads, which are what grow with load.
SERVER_HEADROOM_BYTES = 256 * 1024**2

# The bound when no cgroup limit can be read — a bare `python -m pytest`, a dev box, a container
# run without `--memory`. Deliberately the value this file shipped with, so an unconstrained
# environment behaves exactly as it did before the derivation existed.
UNCONSTRAINED_MEMORY_BYTES = 2 * 1024**3

# Below this a legitimate program cannot run: 629 MiB of address space is what one importing numpy,
# pandas, scipy, sklearn, sympy, matplotlib, RDKit and OpenBabel and drawing a plot actually
# reached, measured. A derivation that lands under it means the pod is too small for the ceiling it
# was given, and that is a deployment defect worth a WARNING rather than a silently unusable tool.
MINIMUM_VIABLE_MEMORY_BYTES = 768 * 1024**2


def container_memory_limit() -> int | None:
    """This process's own cgroup memory limit in bytes, or `None` when it is unbounded.

    The number Linux will OOM-kill this container over — which is the only honest basis for a bound
    that exists to fire *before* it. Both cgroup generations write "no limit" differently: v2 writes
    the literal `max`, v1 writes a number so large it is the kernel's page-counter maximum rather
    than a real limit, so a plain "is it big" test is what distinguishes them.
    """
    for path in (_CGROUP_V2_MAX, _CGROUP_V1_MAX):
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if raw == "max":
            return None
        try:
            value = int(raw)
        except ValueError:  # pragma: no cover — a cgroup file that is not a number.
            continue
        # v1's "unlimited" is `PAGE_COUNTER_MAX` scaled by the page size, which lands in the
        # exabytes. Anything at or above a terabyte is that sentinel rather than a pod's limit.
        return None if value >= 1 << 40 else value
    return None


def default_memory_bytes(max_concurrent_runs: int) -> int:
    """The per-run address-space bound, derived from the pod's own limit and the run ceiling.

    **This is the fix for a guard that could never fire.** `RLIMIT_AS` shipped at a flat 2 GiB
    inside a pod limited to 512Mi — four times the container's own ceiling — so the sandbox's memory
    bound was unreachable: what actually fired was the container OOMKiller, which kills the *pod*
    and every other in-flight MCP session with it, rather than refusing the one offending call. Two
    independently written numbers cannot be kept consistent by review, so only one of them is
    written down now: the pod's limit is the input, and this is the arithmetic over it.

    `RLIMIT_AS` bounds address space, which is always at least resident set, so bounding N runs at
    `(limit - headroom) / N` guarantees they cannot collectively reach the limit the kernel kills
    over. It is conservative in the safe direction — a program is refused at a mapping it might
    never have touched — which is the coarseness `Limits.memory_bytes` has always documented.

    Args:
        max_concurrent_runs: The admission ceiling, i.e. how many of these bounds may be held at
            once. Deriving from it is what makes the guarantee about the *pod* rather than about
            one call.

    Returns:
        The per-run `RLIMIT_AS` value in bytes.
    """
    limit = container_memory_limit()
    if limit is None:
        return UNCONSTRAINED_MEMORY_BYTES
    derived = max(0, limit - SERVER_HEADROOM_BYTES) // max(1, max_concurrent_runs)
    if derived < MINIMUM_VIABLE_MEMORY_BYTES:
        # Reported rather than silently clamped: clamping back up would restore exactly the defect
        # this function exists to end — a bound larger than the pod that therefore never fires.
        logger.warning(
            "pyexec: this pod's memory limit (%d MiB) over %d concurrent runs leaves %d MiB of "
            "address space each, below the %d MiB a program importing the scientific stack needs. "
            "Raise the container's memory limit or lower CHEMCLAW_PYEXEC_MAX_CONCURRENT_RUNS; "
            "legitimate analyses will be refused until then.",
            limit // 1024**2,
            max_concurrent_runs,
            derived // 1024**2,
            MINIMUM_VIABLE_MEMORY_BYTES // 1024**2,
        )
    return derived


@dataclass(frozen=True, slots=True)
class Limits:
    """What one `run_python` call may spend, and how much of its output may come back."""

    wall_seconds: float = 20.0
    """Hard wall clock, enforced by the parent with `killpg`.

    Holds against a program that blocks in an uninterruptible syscall or ignores signals: the parent
    kills the whole process *group* rather than the one pid it launched, so a child in that group
    dies with it.

    **It does not, on its own, reach a child that leaves the group**, and an earlier version of this
    docstring claimed it did. `killpg` targets one process group; a forked grandchild that calls
    `setsid()` (or `setpgid()`) becomes its own session/group leader, and the parent's `killpg` of
    the *original* group never touches it — measured, an orphan outlived the kill by seconds, still
    running, still holding the scratch directory open. What actually forecloses that is
    `process_headroom = 0`: with no headroom the child cannot `fork` at all, so there is no
    grandchild to escape. The wall clock and the fork bound are two halves of one guarantee, not
    one.
    """

    cpu_seconds: int = 15
    """CPU seconds for the user's code, measured from after the libraries are warmed.

    `RLIMIT_CPU` is cumulative over the process's whole life, so the child adds this to what it has
    already spent importing numpy, pandas, scipy and RDKit — otherwise the budget would be mostly
    consumed before the first line of the caller's program ran, and would shrink every time a
    dependency got slower to import.
    """

    memory_bytes: int = UNCONSTRAINED_MEMORY_BYTES
    """`RLIMIT_AS` — address space, not resident set.

    Address space is the bound Linux can enforce without cgroups, and it is coarse: a library that
    reserves a large lazy mapping counts against it while never touching the pages. That coarseness
    is why it is set generously — and why it must still be set *below* the container's own limit,
    which is the ceiling the kernel actually kills over.

    **The default here is the unconstrained one, and the server does not use it.** `tools.py` passes
    `default_memory_bytes(max_concurrent_runs)`, derived from the pod's own cgroup limit, precisely
    because a flat number written here cannot know the pod it will run in: this shipped at 2 GiB
    inside a 512Mi pod, so the guard could never fire and the container OOMKiller — which kills the
    whole pod, taking every other in-flight session with it — was what enforced memory instead. The
    class default stays the flat value for the case with no cgroup limit to read at all: a test, a
    dev box, a container started without `--memory`.
    """

    file_bytes: int = 16 * 1024**2
    """`RLIMIT_FSIZE`. The sandbox has a writable temp directory and nothing else, and this bounds
    what can be put in it — so a program cannot fill the node's disk through the one door it has."""

    open_files: int = 128
    """`RLIMIT_NOFILE`. Descriptor exhaustion is a denial of service against the pod, not just the
    run."""

    process_headroom: int = 0
    """How many *more* tasks than already exist the child may create, via `RLIMIT_NPROC`.

    Expressed as headroom rather than an absolute because Linux counts `RLIMIT_NPROC` per real user
    id and against tasks, threads included — so an absolute number would either be too low to let
    the libraries keep the threads they already made, or high enough to be no bound at all. The
    child reads its own task count after warming and adds this.

    **Zero, deliberately, and this is the other half of the wall-clock guarantee.** The parent's
    `killpg` cannot reach a child that forks and then `setsid`/`setpgid`s out of the killed process
    group (see `wall_seconds`), so the escape is closed at the source: with zero headroom the child
    cannot `fork` at all — there is no grandchild to orphan, and no fork bomb either. Measured as a
    non-root uid (which is how this server runs — `USER 1001`): headroom `16` let a `fork`+`setsid`
    orphan survive the kill; headroom `0` refused the `fork` with `EAGAIN`. `RLIMIT_NPROC` is
    unenforced for root, so this bound relies on the rootless image, exactly as the `RLIMIT_*`
    bounds the child cannot raise back do.

    It costs the child the ability to spawn a *thread* too, and that is affordable here rather than
    incidental: the analysis is single-process arithmetic, the environment pins every BLAS to one
    thread (`OMP_NUM_THREADS=1` and its siblings in `sandbox._environment`), and `threading` and
    `concurrent.futures` are not on `runner.ALLOWED_IMPORTS` — so no legitimate program has a worker
    thread to lose. Measured: a run importing numpy, pandas, `scipy.stats` and RDKit and computing
    over them completes unchanged at headroom `0`.

    **The residual, stated because it is real.** This closes the fork-based escape and the fork
    bomb; it does *not* close a same-uid signal against the parent, nor give the child a private
    network stack. Both need a **PID/NET namespace**, which needs `CAP_SYS_ADMIN` or a user
    namespace — neither promised to a rootless OpenShift pod (`sandbox._seal_from_children` records
    the same limit for `/proc`). The process boundary, the undumpable-parent seal, the in-child
    `socket` neutralisation and the `egress: []` NetworkPolicy remain the backstops for what a
    namespace would otherwise cover.
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
