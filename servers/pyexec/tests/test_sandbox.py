"""What the sandbox refuses, and what it still lets through.

Every other server in this fleet is tested for the answers it gives. This one is tested for the
answers it *denies*, because its input is a program: the failure that matters is not a wrong number
but a run that reached something it should not have.

**The bounds are tightened for the suite and that is deliberate.** `Limits()`'s defaults are sized
for a chemist's analysis; a test that waits 20 s to prove a timeout works is a test somebody deletes
for being slow. `_fast()` shortens the clock and nothing else, so what is under test is the
mechanism rather than the number.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from chemclaw_mcp_pyexec.engine.limits import Limits
from chemclaw_mcp_pyexec.engine.sandbox import run


def _fast(**overrides: Any) -> Limits:
    """The default bounds with a short clock, so the suite proves mechanisms and not patience."""
    return replace(Limits(wall_seconds=8.0, cpu_seconds=4), **overrides)


# --------------------------------------------------------------------------------------------
# The capability itself. If these break, the refusals below are protecting nothing worth having.
# --------------------------------------------------------------------------------------------


def test_arithmetic_comes_back() -> None:
    """The simplest possible run: a value assigned to `result` reaches the caller."""
    outcome = run("result = 6 * 7", limits=_fast())
    assert outcome.error is None
    assert json.loads(outcome.result_json or "null") == 42


def test_data_is_bound_in_the_namespace() -> None:
    """`data` is the only way in, so it has to actually arrive."""
    outcome = run("result = sum(data['xs']) / len(data['xs'])", {"xs": [2, 4, 6]}, _fast())
    assert outcome.error is None
    assert json.loads(outcome.result_json or "null") == 4


def test_data_is_a_dict_even_when_omitted() -> None:
    """A program may subscript `data` unchecked; `None` would make every program defensive."""
    outcome = run("result = list(data.keys())", None, _fast())
    assert outcome.error is None
    assert json.loads(outcome.result_json or "null") == []


def test_printing_is_captured_in_order() -> None:
    """stdout and stderr both come back, because a program's diagnostics are half its output."""
    outcome = run("print('first')\nprint('second')\nresult = None", limits=_fast())
    assert outcome.stdout.splitlines() == ["first", "second"]


def test_numpy_and_pandas_are_available() -> None:
    """The reason this server has a 400 MB image."""
    outcome = run(
        "import numpy as np, pandas as pd\n"
        "df = pd.DataFrame({'x': [1, 2, 3], 'y': [2.0, 4.1, 5.9]})\n"
        "result = float(np.polyfit(df.x, df.y, 1)[0])",
        limits=_fast(),
    )
    assert outcome.error is None
    assert json.loads(outcome.result_json or "null") == pytest.approx(1.95, abs=0.05)


def test_scipy_submodules_import_lazily_inside_a_call() -> None:
    """The regression the first guard design caused, pinned so it cannot come back.

    `scipy.optimize` imports `sys` lazily *at call time*. A `sys.modules` purge plus a `meta_path`
    finder refused it, so `brentq` raised `SandboxImportError` — measured, and the reason the guard
    is a replaced `__import__` instead. A library's imports must not go through the allowlist.
    """
    outcome = run(
        "from scipy import optimize\nresult = float(optimize.brentq(lambda x: x*x - 2, 0, 2))",
        limits=_fast(),
    )
    assert outcome.error is None, outcome.error
    assert json.loads(outcome.result_json or "null") == pytest.approx(2**0.5)


def test_rdkit_is_available() -> None:
    """Structure handling is the chemistry half of what this tool is for."""
    outcome = run(
        "from rdkit import Chem\nresult = Chem.MolToSmiles(Chem.MolFromSmiles('c1ccccc1O'))",
        limits=_fast(),
    )
    assert json.loads(outcome.result_json or "null") == "Oc1ccccc1"


def test_a_dataframe_result_is_encoded_rather_than_repr_ed() -> None:
    """A frame is the realistic large result, so it crosses as data and not as a string."""
    outcome = run(
        "import pandas as pd\nresult = pd.DataFrame({'a': [1, 2], 'b': ['x', 'y']})",
        limits=_fast(),
    )
    decoded = json.loads(outcome.result_json or "null")
    assert decoded["columns"] == ["a", "b"]
    assert decoded["rows"] == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    assert decoded["n_rows"] == 2


def test_a_program_that_raises_is_a_successful_run_carrying_its_traceback() -> None:
    """A caller's bug is data, not an exception here — and the traceback names the sandbox."""
    outcome = run("result = 1 / 0", limits=_fast())
    assert outcome.error is not None
    assert "ZeroDivisionError" in outcome.error
    assert "<analysis>" in outcome.error


# --------------------------------------------------------------------------------------------
# The refusals. Each one is a thing a language model can be talked into writing.
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module",
    ["os", "sys", "socket", "subprocess", "shutil", "pathlib", "importlib", "ctypes", "pickle"],
)
def test_dangerous_modules_are_refused_by_name(module: str) -> None:
    """The allowlist holds against the direct route, and says what is available instead."""
    outcome = run(f"import {module}\nresult = 1", limits=_fast())
    assert outcome.error is not None
    assert "SandboxImportError" in outcome.error
    assert "not available in the analysis sandbox" in outcome.error


def test_the_dunder_import_route_is_refused_too() -> None:
    """`__import__('os')` skips the `import` statement and must not skip the guard with it."""
    outcome = run("result = __import__('os').getcwd()", limits=_fast())
    assert outcome.error is not None
    assert "SandboxImportError" in outcome.error


def test_open_is_not_in_builtins() -> None:
    """No file object is what makes `data` in and `result` out the only channel."""
    outcome = run("result = open('/etc/passwd').read()", limits=_fast())
    assert outcome.error is not None
    assert "name 'open' is not defined" in outcome.error


@pytest.mark.parametrize("name", ["eval", "exec", "compile", "input", "breakpoint"])
def test_the_other_withheld_builtins_are_gone(name: str) -> None:
    """`eval`/`exec`/`compile` in particular would be a second front door past the guard."""
    outcome = run(f"result = {name}", limits=_fast())
    assert outcome.error is not None
    assert f"name '{name}' is not defined" in outcome.error


def test_the_environment_carries_no_credential() -> None:
    """The child's environment is *built*, not filtered, so a token cannot arrive by being new.

    Set a variable shaped exactly like this server's own bearer token and prove the child never sees
    it. Filtering a copy of `os.environ` would pass this test only until somebody added a variable
    nobody thought of — which is why the code builds an allowlist instead, and why this asserts on
    the whole environment rather than on one name.
    """
    os.environ["CHEMCLAW_PYEXEC_TOKEN"] = "a-secret-that-must-not-cross"
    try:
        # `os` is refused inside the sandbox, so the child cannot report its own environment
        # directly. It reports what it *can* reach, and the assertion is that the secret is in
        # neither channel.
        outcome = run("result = repr(dir())", limits=_fast())
        assert "a-secret-that-must-not-cross" not in (outcome.result_json or "")
        assert "a-secret-that-must-not-cross" not in outcome.stdout
    finally:
        del os.environ["CHEMCLAW_PYEXEC_TOKEN"]


# --------------------------------------------------------------------------------------------
# The bounds. These are the controls that hold when the ones above do not.
# --------------------------------------------------------------------------------------------


def test_an_infinite_loop_is_stopped_and_says_so() -> None:
    """Either the CPU limit or the wall clock fires; what matters is that the caller is told."""
    outcome = run("while True:\n    pass", limits=_fast(cpu_seconds=2))
    assert outcome.error is not None
    assert outcome.result_json is None


def test_a_run_that_ignores_the_cpu_signal_still_dies_on_the_wall_clock() -> None:
    """`SIGXCPU` is catchable. The wall clock is not, and it is the bound that always holds.

    The program swallows the CPU signal and keeps spinning, which is a two-line thing to write and
    exactly what a bound that could be caught would be worth. `signal` is not importable, so this
    reaches for it the only way left — and the run still dies.
    """
    outcome = run(
        "import functools\n"
        "try:\n"
        "    while True:\n"
        "        pass\n"
        "except BaseException:\n"
        "    while True:\n"
        "        pass\n",
        limits=_fast(wall_seconds=4.0, cpu_seconds=60),
    )
    assert outcome.timed_out is True
    assert "wall-clock" in (outcome.error or "")


def test_memory_is_bounded() -> None:
    """A single huge allocation is refused rather than taking the node's memory with it."""
    outcome = run("result = len(bytearray(8 * 1024**3))", limits=_fast())
    assert outcome.error is not None
    assert "MemoryError" in outcome.error or "was stopped" in outcome.error


def test_stdout_is_truncated_and_the_truncation_is_reported() -> None:
    """A cap on the caller's context, and one that is never silent."""
    outcome = run("print('A' * 100_000)\nresult = 'done'", limits=_fast(stdout_chars=500))
    assert len(outcome.stdout) == 500
    assert outcome.truncated is True
    assert json.loads(outcome.result_json or "null") == "done"


def test_a_long_frame_is_cut_by_row_and_still_parses() -> None:
    """Truncating a JSON document by character would return something no caller can read."""
    outcome = run(
        "import pandas as pd\nresult = pd.DataFrame({'a': range(1000)})",
        limits=_fast(result_rows=10),
    )
    decoded = json.loads(outcome.result_json or "null")
    assert len(decoded["rows"]) == 10
    assert decoded["n_rows"] == 1000


def test_nothing_survives_between_runs() -> None:
    """Each call is a fresh process. State that leaked would be state a later caller could read."""
    first = run("result = 1\nleaked = 'from the first run'", limits=_fast())
    assert first.error is None
    second = run("result = 'leaked' in dir()", limits=_fast())
    assert json.loads(second.result_json or "null") is False


def test_the_traceback_holds_only_the_callers_frames() -> None:
    """A traceback must name the caller's program and nothing about this server.

    Two things at once. It is what a caller can act on — `runner.py`'s `exec` frame is noise they
    cannot fix. And an unfiltered traceback prints this server's absolute source paths into a
    result a model reads and may quote into an answer, which tells a chemist where the sandbox
    lives for no benefit to either of them.
    """
    outcome = run("def inner():\n    return 1 / 0\n\n\nresult = inner()", limits=_fast())
    assert outcome.error is not None
    assert outcome.error.count("<analysis>") == 2  # the module frame and `inner`
    assert "runner.py" not in outcome.error
    assert "chemclaw_mcp_pyexec" not in outcome.error
    assert outcome.error.rstrip().endswith("ZeroDivisionError: division by zero")


def test_a_syntax_error_returns_the_message_without_a_frame_list() -> None:
    """Nothing ran, so there are no frames — and the message is still the whole fix."""
    outcome = run("result = (", limits=_fast())
    assert outcome.error is not None
    assert "SyntaxError" in outcome.error
    assert "runner.py" not in outcome.error


# --------------------------------------------------------------------------------------------
# The boundary, granting the escape. Every test below *starts* from a program that already holds
# `os` — the guard above does not claim to prevent that — and asks what the boundary does next.
# --------------------------------------------------------------------------------------------

#: A handle on `os` from inside the sandbox. `uuid` is on the allowlist and imports `os` at module
#: level, which is the porosity `runner.py`'s docstring describes rather than a new hole. These
#: tests are about what holds *after* an escape, so they take the shortest reliable route to one
#: instead of the object-graph walk an attacker would write.
_ESCAPED = "import uuid\nos = uuid.os\n"

#: A fake pod secret, shaped like this server's own bearer token — the variable `app.py` reads.
_POD_SECRET = "SECRET-BEARER-abc123"

#: The unprivileged uid the stand-in server drops to when the suite runs as root.
_NOBODY = 65534

#: A stand-in for the pyexec pod, run as a process of its own: it carries a bearer token in its
#: environment, runs one program through `sandbox.run`, and reports what came back beside what the
#: run cost it in memory. It cannot be the pytest process, for three separate reasons — that
#: process's environment is not a pod's, its RSS high-water mark is whatever an earlier test left
#: there, and when the suite runs as root the sandbox child inherits `CAP_SYS_PTRACE`, which reads
#: any `/proc/<pid>` whatever the dumpable flag says.
_STAND_IN_SERVER = """
import ctypes, json, resource, sys

from chemclaw_mcp_pyexec.engine.limits import Limits
from chemclaw_mcp_pyexec.engine.sandbox import run

# `prctl(PR_GET_DUMPABLE)` before anything else, and reported: it is what says this stand-in began
# where a pod begins. A process that had arrived here already sealed — the kernel clears the flag
# on a credential change — would refuse the read below whatever this server did or did not do.
dumpable_at_start = ctypes.CDLL(None).prctl(3, 0, 0, 0, 0)
# The *growth* of the high-water mark, never its value: `execve` carries the forking parent's peak
# into the new process (the kernel folds the old mm's high-water mark into `signal->maxrss`), so
# this process starts out reporting whatever pytest happened to be holding — 56 MiB alone and
# 169 MiB inside the full suite, measured. The difference across one run is this server's own.
before_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
outcome = run(sys.stdin.read(), limits=Limits(wall_seconds=20.0, cpu_seconds=10))
print(
    json.dumps(
        {
            "dumpable_at_start": dumpable_at_start,
            "result_json": outcome.result_json,
            "stdout": outcome.stdout,
            "error": outcome.error,
            "rss_growth_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before_kib,
        }
    )
)
"""


def _drop_privileges() -> None:  # pragma: no cover — runs after fork, inside the stand-in server.
    """Become an unprivileged user, because root reads any `/proc/<pid>` whatever the flag says.

    Nothing here restores the dumpable flag the credential change clears: the `execve` that
    follows sets it back to 1 for an unprivileged image, which is where the pod's own process
    lives and what `dumpable_at_start` above asserts rather than assumes.
    """
    os.setgroups([])
    os.setgid(_NOBODY)
    os.setuid(_NOBODY)


def _through_a_stand_in_server(code: str) -> dict[str, Any]:
    """Run one program through `sandbox.run` in a process standing in for the served pod."""
    completed = subprocess.run(
        [sys.executable, "-c", _STAND_IN_SERVER],
        input=code,
        env={**os.environ, "CHEMCLAW_PYEXEC_TOKEN": _POD_SECRET},
        preexec_fn=_drop_privileges if os.geteuid() == 0 else None,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    answered: dict[str, Any] = json.loads(completed.stdout)
    return answered


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc/<ppid>/environ")
def test_an_escaped_program_cannot_read_the_servers_own_environment() -> None:
    """The child's environment is built from an allowlist; the *parent's* was readable anyway.

    `/proc/<ppid>/environ` is mode 0400 owned by the uid the server runs as — which is the uid the
    child runs as too, so the only thing between an escaped program and this pod's bearer token,
    its DSNs and whatever else a deployment injects is the kernel's rule about the *target's*
    dumpable flag. The stolen values come back to the caller in `result`, an exfiltration channel
    that the empty-egress NetworkPolicy never sees, so the allowlisted environment on its own is
    not the boundary the docstrings claim it is.
    """
    answered = _through_a_stand_in_server(
        _ESCAPED + "fd = os.open('/proc/%d/environ' % os.getppid(), os.O_RDONLY)\n"
        "blob = os.read(fd, 1 << 20).decode('utf-8', 'replace')\n"
        "os.close(fd)\n"
        "result = [entry for entry in blob.split(chr(0)) if 'SECRET-BEARER' in entry]\n"
    )
    # Two guards against a test that passes for the wrong reason: the stand-in has to have started
    # unsealed, and the read has to *fail* rather than merely come back without the token in it.
    assert answered["dumpable_at_start"] == 1, answered
    assert answered["error"] is not None, answered
    assert _POD_SECRET not in json.dumps(answered), answered


def test_a_flood_to_the_stdout_descriptor_does_not_grow_the_server() -> None:
    """The 10,000-character cap is on the runner's own capture; fd 1 goes straight past it.

    A program that reached `os` can `os.write(1, ...)` in a loop, and every byte was read into the
    *parent's* memory and then discarded — a bound on the child's address space that the server
    paid for. Measured before this was fixed: 1.5 GB written took the parent's RSS from 39 MiB to
    3020 MiB, which is an OOM kill of a pod serving every other session, not of the one run.
    """
    answered = _through_a_stand_in_server(
        _ESCAPED + "chunk = b'X' * (1 << 20)\n"
        "for _ in range(512):\n"
        "    os.write(1, chunk)\n"
        "print('the captured channel still answers')\n"
        "result = 'done'\n"
    )
    assert json.loads(answered["result_json"] or "null") == "done", answered["error"]
    assert "the captured channel still answers" in answered["stdout"]
    assert answered["rss_growth_kib"] < 64 * 1024, answered["rss_growth_kib"]


def test_a_flood_to_the_stderr_descriptor_cannot_come_back_as_the_diagnostic() -> None:
    """fd 2 is the same hole as fd 1, and it has a second end: the "died quietly" message.

    The parent needs the child's last stderr line to explain a run that wrote no result, and a
    program that floods fd 2 would otherwise decide how big that explanation is. Writing it to a
    file inside the scratch directory puts it under the child's own `RLIMIT_FSIZE` — a bound it
    cannot raise back — and the parent reads a bounded tail rather than the whole stream.

    The refusal reaches the program as `OSError: File too large` rather than killing it, because
    CPython ignores `SIGXFSZ` so that an over-long write returns `EFBIG` instead. That is the
    better half of the outcome: the caller is told which bound stopped their program.
    """
    outcome = run(
        _ESCAPED + "chunk = b'X' * (1 << 20)\n"
        "for _ in range(64):\n"
        "    os.write(2, chunk)\n"
        "result = 'done'\n",
        limits=_fast(),
    )
    assert outcome.result_json is None, "the flood was absorbed by the server rather than refused"
    assert outcome.error is not None
    assert "File too large" in outcome.error
    assert len(outcome.error) < 8_000, len(outcome.error)


def test_each_call_gets_its_own_scratch_directory_and_leaves_none_behind() -> None:
    """One directory per call, and it is `HOME`, `TMPDIR` and the working directory at once.

    This is the half of statelessness that the code holds up: what an *escaped* program writes to
    an absolute path elsewhere in the pod is bounded by the deployment and by nothing here, which
    is why the README says so rather than promising that nothing survives a call.
    """
    first = run(
        _ESCAPED + "result = [os.getcwd(), os.environ['TMPDIR'], os.environ['HOME']]",
        limits=_fast(),
    )
    second = run(_ESCAPED + "result = os.getcwd()", limits=_fast())
    working, tmpdir, home = json.loads(first.result_json or "null")
    assert working == tmpdir == home
    assert json.loads(second.result_json or "null") != working
    assert not Path(working).exists()
    assert not Path(json.loads(second.result_json or "null")).exists()


def test_the_scratch_directory_goes_even_when_the_run_is_killed() -> None:
    """The error path is the one that matters: a killed run must not leave its directory behind."""
    scratch = Path(tempfile.gettempdir())
    before = set(scratch.glob("pyexec-*"))
    outcome = run("while True:\n    pass", limits=_fast(wall_seconds=2.0, cpu_seconds=60))
    assert outcome.timed_out is True
    assert set(scratch.glob("pyexec-*")) == before
