"""The parent half of the sandbox: launch a child, bound it, kill it if it overstays, read it back.

This process holds the server's credentials, sockets and event loop, so nothing here may be
reachable by a caller's program; only a JSON file crosses.

- **Kill by process group.** The child gets its own session (`start_new_session=True`) and a timeout
  sends `SIGKILL` to the whole group; a grandchild that escapes the group via `setsid` cannot exist
  because `Limits.process_headroom = 0` forbids forking.
- **The environment is built from an allowlist, never filtered**, so no `CHEMCLAW_*`, token or DSN
  reaches the child.
- **The parent seals itself** (`_seal_from_children`) so a same-uid child cannot read
  `/proc/<ppid>/environ`.
- **stdout goes to `/dev/null`, stderr to a file** in the scratch directory, bounded by the child's
  `RLIMIT_FSIZE`, so a program cannot spend the parent's memory through a pipe.
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

#: Environment variables copied into the child, and the complete list: `PATH` to find the
#: interpreter, the locale pair for deterministic text handling. Everything else is set in
#: `_environment`.
_INHERITED = ("PATH", "LANG", "LC_ALL")

#: `prctl(2)`'s `PR_SET_DUMPABLE`; the standard library has no `prctl` binding.
_PR_SET_DUMPABLE = 4

#: How much of the child's stderr the parent reads back when a run left no result. Bounds this
#: process's read so a program flooding fd 2 cannot choose the size of its error message.
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

    BLAS threads are pinned to one so `cpu_seconds` means the same budget on every node rather than
    being divided across cores.
    """
    environment = {name: os.environ[name] for name in _INHERITED if name in os.environ}
    environment.update(
        {
            "HOME": str(home),
            "TMPDIR": str(home),
            # A transitive Matplotlib import must not try to reach a display and hang.
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

    The child runs as the same uid, so without this it could read `/proc/<ppid>/environ` (the bearer
    token, DSNs) and return it through `result`, a route the egress policy cannot see.
    `PR_SET_DUMPABLE=0` re-owns `/proc/<pid>/…` to root, so the child gets `EACCES`; it needs no
    capability, unlike a PID namespace. Called per run (cheap and idempotent). Root bypasses it,
    which is one reason the image runs as `USER 1001`.
    """
    if not sys.platform.startswith("linux"):  # pragma: no cover — the deployment is Linux.
        # Both `/proc/<pid>/environ` and `prctl` are Linux-only; elsewhere there is nothing to seal.
        return
    if ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "could not make the sandbox's parent process undumpable")


def _stderr_tail(path: Path) -> str:
    """The last `_STDERR_TAIL_BYTES` of the child's stderr, decoded permissively.

    Only the tail, so a flooded stderr file cannot spend the parent's memory.
    """
    with path.open("rb") as source:
        source.seek(0, os.SEEK_END)
        source.seek(max(0, source.tell() - _STDERR_TAIL_BYTES))
        return source.read().decode("utf-8", "replace")


def _diagnostic_suffix(path: Path) -> str:
    """The last line the child wrote to stderr, rendered for a log line, or `""`.

    Empty when the child wrote nothing, which is typical for a timeout.
    """
    tail = _stderr_tail(path).strip().splitlines()
    return f"; last stderr line: {tail[-1]}" if tail else ""


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    """SIGKILL the child's whole process group, tolerating a child that has already gone.

    The group id is read with `os.getpgid` rather than assumed equal to the pid.
    """
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):  # pragma: no cover — a race with normal exit.
        process.kill()


def run(code: str, data: dict[str, object] | None = None, limits: Limits | None = None) -> Outcome:
    """Run `code` in a bounded child process and return what it produced.

    Synchronous on purpose: `tools.py` hands it to a worker thread.

    Args:
        code: The program. Runs with `data` and `result` already bound in its namespace.
        data: JSON-serialisable values to bind as `data`. `None` binds an empty dict.
        limits: The bounds to run inside. `None` takes the defaults.

    Returns:
        An `Outcome`. A program that raised is a successful run carrying an `error`.

    Raises:
        RuntimeError: The runner itself failed to start or wrote nothing (a server defect).
        OSError: This process could not be sealed against the child; refusing to run fails closed.
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
            # S603: the argv is this interpreter plus our own runner; the caller's code travels in
            # the payload file, never on the command line.
            process = subprocess.Popen(  # noqa: S603
                # `-I`: no `PYTHON*` env, no user site, no script dir on `sys.path`. `-B`: no
                # bytecode.
                [sys.executable, "-I", "-B", str(_RUNNER), str(payload), str(result)],
                cwd=scratch,
                env=_environment(home),
                # Printed output returns in `result.json`; a stdout pipe would only let a program
                # spend this process's memory. stderr goes to a file bounded by the child's
                # `RLIMIT_FSIZE`.
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
            # Counted: a rate of timeouts means the limit is too tight or programs do not terminate.
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
            # The child died without writing (a resource limit or a crash). The reason must reach
            # the caller, without quoting stderr to them.
            detail = _stderr_tail(diagnostics).strip().splitlines()
            tail = detail[-1] if detail else f"exit status {process.returncode}"
            # `killed` is its own outcome: it is this pod's ceiling, for an operator, where `error`
            # is the program's.
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
        # Not logged: that would put submitted-program text in the pod log.
        RUNS.labels("error" if written["error"] else "ok").inc()
        return Outcome(
            stdout=str(written["stdout"]),
            result_json=written["result_json"],
            error=written["error"],
            truncated=bool(written["truncated"]),
            timed_out=False,
        )
