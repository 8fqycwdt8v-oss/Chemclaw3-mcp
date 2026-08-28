"""The parent half of the sandbox: launch a child, bound it, kill it if it overstays, read it back.

`engine/runner.py` is the code that runs inside; this is the code that decides what "inside" means.
The split is not cosmetic — everything here happens in a process that still holds the server's
credentials, its sockets and its event loop, so nothing here may ever be reached by a caller's
program. The only thing that crosses is a JSON file.

**The kill is by process group, and that is the lesson this repository already paid for.** The
`calc` server's `run_isolated` records it against `xtb`: `subprocess.run(argv, timeout=...)` kills
the one pid it launched, and any child that pid spawned keeps running — still burning CPU, still
writing into the temp directory being cleaned up around it. So the child is started with
`start_new_session=True`, which gives it a session and a process group of its own, and a timeout
sends `SIGKILL` to the whole group.

**The group kill reaches only what stays in the group, and that is why it is paired with a fork
bound.** A grandchild that forks and then `setsid`/`setpgid`s becomes its own session leader, and
this `killpg` of the *original* group never touches it — an orphan outlived the kill in a measured
escape. The half that forecloses it is `Limits.process_headroom = 0` in `runner.py`: the child
cannot `fork` at all, so no such grandchild exists. See `Limits.wall_seconds`/`process_headroom`.

**The environment is built, never filtered.** Deleting known-dangerous variables from a copy of
`os.environ` fails the moment somebody adds a variable nobody thought of, and this server runs in a
pod whose environment carries a bearer token for itself. So the child's environment is assembled
from an allowlist of five names and contains nothing else — no `CHEMCLAW_*`, no `*_TOKEN`, no DSN.

**And that is only half of it, which is what an escape proved.** A clean child environment says
nothing about *this* process's, and the two run as the same uid: a program that reached `os` read
`/proc/<ppid>/environ` and got this pod's bearer token back out through `result` — past the empty
`egress:` the design leans on, because the answer leaves by the route the caller came in. So the
parent seals itself before it forks (`_seal_from_children`). The allowlist stays: it is what makes
the *child* boring, and the seal is what makes the parent unreadable.

**The child's stdout goes nowhere and its stderr goes to a file**, for the same class of reason.
Reading the child's stdout into this process meant a program writing straight to fd 1 chose how
much of the server's memory to spend — measured at 1.5 GB written for +2981 MiB of parent RSS,
which is an OOM kill of every session the pod is serving. Nothing here ever wanted those bytes
(the runner captures its own output and returns it in `result.json`), and the diagnostic the
parent does want is one line, so stderr lands in a file inside the scratch directory, where the
child's own `RLIMIT_FSIZE` bounds it and a tail read bounds what comes back.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from chemclaw_mcp_pyexec.engine.limits import Limits
from chemclaw_mcp_pyexec.engine.metrics import RUNS

logger = logging.getLogger(__name__)

__all__ = ["Outcome", "run"]

_RUNNER = Path(__file__).with_name("runner.py")

#: Environment variables copied into the child, and the complete list of them.
#:
#: `PATH` so the interpreter can be found; the locale pair so text handling is deterministic rather
#: than inherited. Everything else the child needs is set explicitly in `_environment` below.
_INHERITED = ("PATH", "LANG", "LC_ALL")

#: `prctl(2)`'s `PR_SET_DUMPABLE`. The number rather than a binding because there is no `prctl` in
#: the standard library, and a one-constant `ctypes` call is smaller than a dependency.
_PR_SET_DUMPABLE = 4

#: How much of the child's stderr the parent reads back when a run left no result.
#:
#: One line is what `run` reports, so this only has to be long enough to contain it. It is a bound
#: on this process rather than on the child — the child's `RLIMIT_FSIZE` already bounds the file —
#: and it is what keeps a program that floods fd 2 from choosing the size of its own error message.
_STDERR_TAIL_BYTES = 4096


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a run produced. Always returned; `run` raises only if the runner itself failed."""

    stdout: str
    result_json: str | None
    error: str | None
    truncated: bool
    timed_out: bool


def _environment(home: Path) -> dict[str, str]:
    """The child's whole environment.

    The thread pinning is not a performance tweak. A BLAS that opens one thread per core inside a
    process whose CPU budget is counted across all of them turns `cpu_seconds` into
    `cpu_seconds / cores`, which would make the limit mean something different on every node. One
    thread makes the budget mean what it says.
    """
    environment = {name: os.environ[name] for name in _INHERITED if name in os.environ}
    environment.update(
        {
            "HOME": str(home),
            "TMPDIR": str(home),
            # Matplotlib is not offered, but a transitive import of it must not try to reach a
            # display and hang the run before the wall clock notices.
            "MPLBACKEND": "Agg",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
        }
    )
    return environment


def _seal_from_children() -> None:
    """Make this process's `/proc` entry unreadable to the child it is about to start.

    `/proc/<pid>/environ` is mode 0400 owned by the uid of the process it describes, and the child
    runs as that same uid — so "the child's environment holds no credential" was never the whole
    claim it read as: an escaped program took `os.getppid()` and read this server's bearer token,
    its DSNs and everything else the deployment injects, then returned them to the caller in
    `result`. That is exfiltration by the route the request came in on, which the empty-egress
    NetworkPolicy cannot see.

    `PR_SET_DUMPABLE=0` re-owns `/proc/<pid>/…` to root, so the same-uid child is refused with
    `EACCES`. It is the portable half of the pair: a PID namespace hides `/proc/<ppid>` outright
    but needs `CAP_SYS_ADMIN` or an unprivileged user namespace, and a rootless OpenShift pod is
    promised neither. The one thing it costs is a core dump of this process, which for a process
    holding a bearer token is not a cost.

    Called per run rather than once at import: it is cheap (a single syscall), it is idempotent,
    and tying it to the moment a hostile child exists is what keeps it true if this ever runs
    behind a forking worker. Root is exempt from the rule — `CAP_SYS_PTRACE` reads any `/proc`
    whatever the flag says — which is one more reason the image ships `USER 1001`.
    """
    if not sys.platform.startswith("linux"):  # pragma: no cover — the deployment is Linux.
        # `/proc/<pid>/environ` is a Linux interface, and so is `prctl`. There is nothing here to
        # seal on a platform that has neither, and pretending otherwise would be the failure this
        # function exists to fix, one level up.
        return
    if ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "could not make the sandbox's parent process undumpable")


def _stderr_tail(path: Path) -> str:
    """The last `_STDERR_TAIL_BYTES` of the child's stderr, decoded permissively.

    The tail rather than the file: a program that writes to fd 2 in a loop is bounded by its own
    `RLIMIT_FSIZE` at 16 MiB, and reading 16 MiB in order to quote one line of it would hand that
    program the parent's memory anyway, which is exactly the bypass this replaced.
    """
    with path.open("rb") as source:
        source.seek(0, os.SEEK_END)
        source.seek(max(0, source.tell() - _STDERR_TAIL_BYTES))
        return source.read().decode("utf-8", "replace")


def _diagnostic_suffix(path: Path) -> str:
    """The last line the child wrote to stderr, rendered for a log line, or `""`.

    A timed-out run has usually written nothing — it was killed mid-work — so this stays empty
    rather than padding every WARNING with an empty parenthesis. When there *is* a line it is the
    only evidence of what the program was doing, and it belongs beside the kill rather than only in
    a file inside a scratch directory that is about to be removed.
    """
    tail = _stderr_tail(path).strip().splitlines()
    return f"; last stderr line: {tail[-1]}" if tail else ""


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    """SIGKILL the child's whole process group, tolerating a child that has already gone.

    `os.getpgid` is read rather than assuming the pid *is* the group id: it is, because of
    `start_new_session=True`, and reading it means this stays correct if that ever changes rather
    than killing a group we do not own.
    """
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):  # pragma: no cover — a race with normal exit.
        process.kill()


def run(code: str, data: dict[str, object] | None = None, limits: Limits | None = None) -> Outcome:
    """Run `code` in a bounded child process and return what it produced.

    Synchronous on purpose. A run is up to `wall_seconds` of CPU-bound work in another process, so
    the caller — `tools.py` — hands it to a worker thread rather than letting it sit on the event
    loop; making this `async` would only move that decision somewhere it is easier to get wrong.

    Args:
        code: The program. Runs with `data` and `result` already bound in its namespace.
        data: JSON-serialisable values to bind as `data`. `None` binds an empty dict, never `None`,
            so a program can always subscript it.
        limits: The bounds to run inside. `None` takes the defaults, which is what the tool passes.

    Returns:
        An `Outcome`. A program that raised is a *successful* run carrying an `error` — the caller
        needs to see the traceback, and turning it into an exception here would only mean
        reconstructing it there.

    Raises:
        RuntimeError: The runner itself failed — it could not start, or it wrote nothing. That is a
            defect in this server rather than in the caller's program, and it is not reported as
            one.
        OSError: This process could not be sealed against the child it is about to start. Refusing
            to run is the only safe answer: the alternative is a run whose boundary is one control
            short of what every docstring here says it is.
    """
    bounds = limits or Limits()
    _seal_from_children()
    with tempfile.TemporaryDirectory(prefix="pyexec-") as scratch:
        home = Path(scratch)
        payload = home / "payload.json"
        result = home / "result.json"
        diagnostics = home / "stderr.log"
        payload.write_text(
            json.dumps({"code": code, "data": data or {}, "limits": bounds.as_dict()}),
            encoding="utf-8",
        )

        with diagnostics.open("wb") as sink:
            process = subprocess.Popen(
                # `-I` isolates: no `PYTHON*` environment, no user site-packages, and no script
                # directory on `sys.path`. `-B` keeps the child from writing bytecode into an image
                # whose filesystem is read-only anyway.
                [sys.executable, "-I", "-B", str(_RUNNER), str(payload), str(result)],
                cwd=scratch,
                env=_environment(home),
                # Nothing here reads the child's stdout: what a program printed is captured by the
                # runner and comes back in `result.json`, so a pipe only offered a program a way to
                # spend this process's memory. `stderr` is wanted — one line of it, when a run left
                # no result — and a file inside the scratch directory is where the child's own
                # `RLIMIT_FSIZE` bounds what it can put there.
                stdout=subprocess.DEVNULL,
                stderr=sink,
                start_new_session=True,
            )
            timed_out = False
            try:
                process.wait(timeout=bounds.wall_seconds)
            except subprocess.TimeoutExpired:
                _kill_group(process)
                process.wait()
                timed_out = True

        if timed_out:
            # WARNING and counted. A process group killed for going over its wall clock is the
            # sandbox's bound doing its job, but a *rate* of them is a deployment fact — the limit
            # is too tight for the analyses being asked for, or something is submitting programs
            # that do not terminate — and neither was observable from outside this pod.
            RUNS.labels("timeout").inc()
            logger.warning(
                "pyexec run exceeded its %gs wall-clock limit; SIGKILLed its process group%s",
                bounds.wall_seconds,
                _diagnostic_suffix(diagnostics),
            )
            return Outcome(
                stdout="",
                result_json=None,
                error=f"the analysis exceeded its {bounds.wall_seconds:g}s wall-clock limit",
                truncated=False,
                timed_out=True,
            )

        if not result.is_file():
            # The child died without writing: killed by a resource limit (SIGXCPU, the OOM killer),
            # or crashed. Its stderr is the only evidence and is this server's to explain, not the
            # caller's to read — but the *reason* has to reach them or an answer silently loses a
            # step it thinks it took.
            detail = _stderr_tail(diagnostics).strip().splitlines()
            tail = detail[-1] if detail else f"exit status {process.returncode}"
            # The branch that means a *resource* limit fired rather than a program failing, and it
            # was silent: the caller got one sentence and this pod recorded nothing. `killed` is
            # deliberately its own outcome rather than folded into `error`, because the responses
            # differ — an `error` is the submitted program's problem, a `killed` is this pod's
            # memory or CPU ceiling and is the one an operator has to act on.
            RUNS.labels("killed").inc()
            logger.warning(
                "pyexec run was stopped before it finished: exit=%s %s",
                process.returncode,
                tail,
            )
            return Outcome(
                stdout="",
                result_json=None,
                error=f"the analysis was stopped before it finished ({tail})",
                truncated=False,
                timed_out=False,
            )

        written = json.loads(result.read_text(encoding="utf-8"))
        # A program that raised is a *successful* run carrying a traceback (see this function's
        # Returns), so it is counted apart from one that produced an answer and apart from the two
        # ways the sandbox stopped it. Not logged: a caller's own `ZeroDivisionError` is the
        # caller's to read, and logging every one would put submitted-program text in the pod log.
        RUNS.labels("error" if written["error"] else "ok").inc()
        return Outcome(
            stdout=str(written["stdout"]),
            result_json=written["result_json"],
            error=written["error"],
            truncated=bool(written["truncated"]),
            timed_out=False,
        )
