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

**The environment is built, never filtered.** Deleting known-dangerous variables from a copy of
`os.environ` fails the moment somebody adds a variable nobody thought of, and this server runs in a
pod whose environment carries a bearer token for itself. So the child's environment is assembled
from an allowlist of five names and contains nothing else — no `CHEMCLAW_*`, no `*_TOKEN`, no DSN.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from chemclaw_mcp_pyexec.engine.limits import Limits

__all__ = ["Outcome", "run"]

_RUNNER = Path(__file__).with_name("runner.py")

#: Environment variables copied into the child, and the complete list of them.
#:
#: `PATH` so the interpreter can be found; the locale pair so text handling is deterministic rather
#: than inherited. Everything else the child needs is set explicitly in `_environment` below.
_INHERITED = ("PATH", "LANG", "LC_ALL")


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
    """
    bounds = limits or Limits()
    with tempfile.TemporaryDirectory(prefix="pyexec-") as scratch:
        home = Path(scratch)
        payload = home / "payload.json"
        result = home / "result.json"
        payload.write_text(
            json.dumps({"code": code, "data": data or {}, "limits": bounds.as_dict()}),
            encoding="utf-8",
        )

        process = subprocess.Popen(
            # `-I` isolates: no `PYTHON*` environment, no user site-packages, and no script
            # directory on `sys.path`. `-B` keeps the child from writing bytecode into an image
            # whose filesystem is read-only anyway.
            [sys.executable, "-I", "-B", str(_RUNNER), str(payload), str(result)],
            cwd=scratch,
            env=_environment(home),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        timed_out = False
        try:
            _, stderr = process.communicate(timeout=bounds.wall_seconds)
        except subprocess.TimeoutExpired:
            _kill_group(process)
            _, stderr = process.communicate()
            timed_out = True

        if timed_out:
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
            detail = stderr.decode("utf-8", "replace").strip().splitlines()
            tail = detail[-1] if detail else f"exit status {process.returncode}"
            return Outcome(
                stdout="",
                result_json=None,
                error=f"the analysis was stopped before it finished ({tail})",
                truncated=False,
                timed_out=False,
            )

        written = json.loads(result.read_text(encoding="utf-8"))
        return Outcome(
            stdout=str(written["stdout"]),
            result_json=written["result_json"],
            error=written["error"],
            truncated=bool(written["truncated"]),
            timed_out=False,
        )
