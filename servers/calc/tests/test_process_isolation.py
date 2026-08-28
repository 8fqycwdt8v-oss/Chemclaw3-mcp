"""The timeout that stops a runaway calculation actually reaches everything it spawned.

`run_isolated` exists for one reason, stated in its own docstring: `subprocess.run(argv, timeout=…)`
kills only the PID it tracks, and `xtb` forks workers that are not in that process's group — so a
timed-out run left orphans "still burning CPU, and still writing into the tempdir after the caller's
`TemporaryDirectory.__exit__` has removed it".

That argument was written, merged, and never tested. Nothing stopped an edit back to the naive form,
and the regression would be **silent** in the worst way: the call still raises `TimeoutExpired` on
time, the caller still gets its error, the tool still looks bounded — and a `crest` search goes on
consuming a pod's CPU with nobody waiting for the answer. It is the shape this fleet keeps finding:
a control that is recorded as enabled and is not there.

So both directions are pinned. `test_the_naive_form_leaks_a_forked_worker` is what makes the other
test mean something: without it, "the worker is gone" could just as well be true of
`subprocess.run`, and the assertion would pass against the very code this function replaced.

**Why the check is `/proc` state and not `os.kill(pid, 0)`.** A signal probe succeeds against a
*zombie*, and an orphan whose parent has just been killed is reaped by PID 1 — which, in a container
without a real init, may never happen. Measured while writing this: the naive form and the isolated
form were indistinguishable under `os.kill(pid, 0)`, and the isolated one looked broken when it was
not. `Z` means the kill landed; a running state means it did not.

Linux-only, deliberately: the fleet's servers are Linux containers, and `/proc` is what can tell a
zombie from a live process without racing a reaper.
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
    """And `subprocess.run(timeout=…)` does not — which is what makes the test above a test.

    Not a test of the standard library so much as of the *premise*. If this ever stops leaking,
    `run_isolated`'s reason for existing has gone with it, and the honest response is to read this
    file and decide — not to keep a wrapper whose docstring argues against a hazard that no longer
    exists.
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
    """The `ProcessLookupError` branch: nothing was killed, so nothing may say one was.

    `os.getpgid` raising means the process is already gone between the timeout and the kill — a
    race, not an error. The branch handled it by seeding `killed = -1` and incrementing
    `PROCESS_GROUP_KILLS` *outside* the `suppress`, so the run booked a kill it had not made and
    logged `SIGKILLed process group -1`. Measured before the fix, with `getpgid` raising:
    `chemclaw_mcp_calc_process_group_kills_total{binary="python3"} 1.0` for a run nothing killed.

    That counter is what an operator reads as "this pod is killing calculations", which is the
    signal for an undersized budget or an oversized molecule. A count of events that did not happen
    is worse than no count: it is a decision made on a number nobody can reproduce.

    The timeout itself must still be reported — the caller's budget really is spent — so
    `TimeoutExpired` and `chemclaw_mcp_calc_subprocess_timeouts_total` are asserted in the same
    breath as the kill's absence.
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
