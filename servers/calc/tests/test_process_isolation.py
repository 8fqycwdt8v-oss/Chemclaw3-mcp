"""The timeout that stops a runaway calculation actually reaches everything it spawned.

`subprocess.run(timeout=...)` kills only the tracked PID, and `xtb` forks workers outside it, so
a timed-out run would leave orphans burning CPU. `run_isolated` kills the whole process group.
Both directions are pinned: the naive form leaks, which is what makes the isolated form's test
meaningful.

State is read from `/proc` rather than `os.kill(pid, 0)`, which succeeds against a zombie: `Z`
means the kill landed. Linux-only, as the fleet's containers are.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from chemclaw_mcp_calc.engine.xtb_cli import run_isolated

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="reads /proc to tell a zombie from a live process"
)

# Stands in for `xtb`: forks a worker that outlives any bound under test, records both pids, then
# blocks. The fork is the whole point — it is what a naive timeout fails to reach.
_FORKING_ENGINE = """
import os, sys, time
worker = os.fork()
if worker == 0:
    time.sleep(300)
    os._exit(0)
open(sys.argv[1], "w").write(f"{worker} {os.getpgid(worker)} {os.getpgid(0)}")
sys.stdout.flush()
time.sleep(300)
"""

# Long enough that a worker still alive is alive because nothing killed it, not because a signal is
# in flight; short enough that the file stays quick.
_SETTLE_SECONDS = 1.0
_BOUND_SECONDS = 2.0


def _process_state(pid: int) -> str:
    """`R`/`S`/`D`/`Z`/`T` from `/proc/<pid>/stat`, or `gone` once the entry has disappeared.

    The state is the field after the closing parenthesis of `comm`, which is split on from the
    *right* because a process name may itself contain one.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return "gone"
    return stat.rsplit(")", 1)[1].split()[0]


def _is_dead(pid: int) -> bool:
    """Whether `pid` has stopped running — reaped, or a zombie awaiting one."""
    return _process_state(pid) in {"gone", "Z"}


def _kill(pid: int) -> None:
    """Best-effort cleanup, so a leak this file *proves* does not outlive the test run."""
    try:
        os.kill(pid, 9)
    except (ProcessLookupError, PermissionError):
        return


def _run_forking_engine(runner: str) -> tuple[int, int, int]:
    """Run the forking stand-in under `runner` until it times out; return (worker, wpgid, cpgid).

    `runner` is `"isolated"` or `"naive"` — the function under test and the form it replaced,
    driven through the same script so the only difference is how the timeout is enforced.
    """
    with tempfile.TemporaryDirectory() as directory:
        script = Path(directory) / "forking_engine.py"
        script.write_text(_FORKING_ENGINE)
        pidfile = Path(directory) / "worker.pid"
        argv = [sys.executable, str(script), str(pidfile)]
        env = {"PATH": os.environ.get("PATH", "")}
        with pytest.raises(subprocess.TimeoutExpired):
            if runner == "isolated":
                run_isolated(
                    argv,
                    cwd=Path(directory),
                    env=env,
                    timeout=_BOUND_SECONDS,
                    label="forking-engine",
                )
            else:
                # The naive form, run here deliberately: this branch exists to prove it leaks.
                subprocess.run(
                    argv,
                    cwd=directory,
                    env=env,
                    timeout=_BOUND_SECONDS,
                    capture_output=True,
                    text=True,
                    check=False,
                )
        worker, worker_pgid, child_pgid = (int(part) for part in pidfile.read_text().split())
    time.sleep(_SETTLE_SECONDS)
    return worker, worker_pgid, child_pgid


def test_a_timed_out_run_takes_every_process_it_spawned_with_it() -> None:
    """The property `run_isolated` exists for: the fork dies with the run, not after it."""
    worker, worker_pgid, child_pgid = _run_forking_engine("isolated")
    try:
        assert worker_pgid == child_pgid, (
            "the forked worker is not in the run's process group, so `killpg` could not have "
            "reached it — `start_new_session=True` is what puts it there"
        )
        assert _is_dead(worker), (
            f"the forked worker (pid {worker}, state {_process_state(worker)!r}) survived the "
            "timeout: it is still burning CPU for an answer nobody is waiting for, which is "
            "exactly what run_isolated replaced subprocess.run to prevent"
        )
    finally:
        _kill(worker)


def test_the_naive_form_leaks_a_forked_worker() -> None:
    """`subprocess.run(timeout=...)` does leak a forked worker, which makes the test above a test.

    If this ever stops leaking, `run_isolated`'s premise is gone and should be reconsidered.
    """
    worker, _worker_pgid, _child_pgid = _run_forking_engine("naive")
    try:
        assert not _is_dead(worker), (
            "subprocess.run(timeout=…) now reaps a forked worker as well, so the hazard "
            "run_isolated was written for may be gone; re-read xtb_cli.run_isolated before "
            "relaxing anything"
        )
    finally:
        _kill(worker)


def test_a_kill_that_did_not_happen_is_neither_counted_nor_claimed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A kill that did not happen is neither counted nor claimed.

    `os.getpgid` raising means the process already exited, a race rather than an error, so no kill
    is counted or logged; an operator reads that counter as "calculations being killed". The timeout
    itself is still reported, since the budget really is spent.
    """
    import logging

    from chemclaw_mcp_calc.engine.metrics import PROCESS_GROUP_KILLS, SUBPROCESS_TIMEOUTS

    def already_gone(_pid: int) -> int:
        raise ProcessLookupError

    monkeypatch.setattr(os, "getpgid", already_gone)
    # `run_isolated` labels by the basename of `argv[0]`, which for this stand-in is the
    # interpreter's own name rather than `xtb`.
    binary = Path(sys.executable).name
    kills = PROCESS_GROUP_KILLS.labels(binary)._value.get()
    timeouts = SUBPROCESS_TIMEOUTS.labels(binary)._value.get()

    with tempfile.TemporaryDirectory() as directory:
        argv = [sys.executable, "-c", "import time; time.sleep(30)"]
        with caplog.at_level(logging.WARNING), pytest.raises(subprocess.TimeoutExpired):
            run_isolated(
                argv,
                cwd=Path(directory),
                env={"PATH": os.environ.get("PATH", "")},
                timeout=0.3,
                label="single point",
            )

    assert PROCESS_GROUP_KILLS.labels(binary)._value.get() == kills, (
        "a run whose process group was never killed booked a kill; that counter is the one an "
        "operator reads as 'this pod is killing calculations'"
    )
    assert SUBPROCESS_TIMEOUTS.labels(binary)._value.get() == timeouts + 1, (
        "the timeout itself must still be counted — the caller's budget really was spent"
    )
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "process group -1" not in logged, f"the warning claimed a kill it did not make: {logged}"
    assert "no process group was killed" in logged
