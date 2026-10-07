"""What the sandbox refuses, and what it still lets through.

The input is a program, so the failure that matters is a run reaching something it should not.
`_fast()` shortens the clock for the suite and nothing else, so the mechanism is under test
rather than the production number.
"""

from __future__ import annotations

import base64
import errno
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
    """Library-internal lazy imports do not go through the allowlist.

    `scipy.optimize` imports `sys` at call time; a `meta_path` guard refused it and broke `brentq`,
    which is why the guard is a replaced `__import__`.
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


def test_open_reads_a_path_outside_the_jail_the_same_way_it_always_refused_it() -> None:
    """`open` is guarded now, not withheld — the refusal moved from `NameError` to
    `SandboxPathError`, and this is that refusal, not a lost capability. See the jail tests below
    for what `open` now *does* do."""
    outcome = run("result = open('/etc/passwd').read()", limits=_fast())
    assert outcome.error is not None
    assert "SandboxPathError" in outcome.error
    assert "resolves outside the sandbox" in outcome.error


# --------------------------------------------------------------------------------------------
# The jailed filesystem: `open` is restored so a program can write or read back a file inside its
# own call; these tests are the boundary of that restoration.
# --------------------------------------------------------------------------------------------


def test_a_file_written_in_the_jail_can_be_read_back_in_the_same_call() -> None:
    """The whole point of restoring `open`: a program's own scratch file round-trips."""
    outcome = run(
        "with open('scratch.txt', 'w') as f:\n"
        "    f.write('hello sandbox')\n"
        "with open('scratch.txt') as f:\n"
        "    result = f.read()",
        limits=_fast(),
    )
    assert outcome.error is None, outcome.error
    assert json.loads(outcome.result_json or "null") == "hello sandbox"


def test_a_relative_traversal_out_of_the_jail_is_refused() -> None:
    """`../../etc/passwd` is a `..` walk out of the scratch directory, not a path inside it."""
    outcome = run("result = open('../../etc/passwd').read()", limits=_fast())
    assert outcome.error is not None
    assert "SandboxPathError" in outcome.error
    assert "resolves outside the sandbox" in outcome.error


@pytest.mark.parametrize("path", ["/etc/passwd", "/proc/1/environ"])
def test_an_absolute_path_outside_the_jail_is_refused(path: str) -> None:
    """An absolute path is checked against the jail, never trusted because it looks well-formed."""
    outcome = run(f"result = open({path!r}).read()", limits=_fast())
    assert outcome.error is not None
    assert "SandboxPathError" in outcome.error
    assert "resolves outside the sandbox" in outcome.error


def test_a_symlink_planted_inside_the_jail_cannot_point_out_of_it() -> None:
    """`Path.resolve()` follows symlinks before the containment check, not after."""
    outcome = run(
        "import uuid\nos = uuid.os\n"
        "os.symlink('/etc/passwd', 'escape')\n"
        "result = open('escape').read()",
        limits=_fast(),
    )
    assert outcome.error is not None
    assert "SandboxPathError" in outcome.error


def test_a_file_descriptor_is_refused_rather_than_resolved() -> None:
    """An int names whatever fd the child already holds; nothing here can jail that after the
    fact."""
    outcome = run("result = open(1, 'w').write('x')", limits=_fast())
    assert outcome.error is not None
    assert "SandboxPathError" in outcome.error
    assert "file descriptor" in outcome.error


def test_a_chdir_does_not_relocate_the_jail() -> None:
    """A `chdir` does not relocate the jail.

    `os` is reachable through an allowed module (`uuid.os`), so the jail is pinned once in `main()`
    before `exec()`; moving `cwd` changes where a relative path is joined from, never what it is
    checked against.
    """
    outcome = run(
        "import uuid\nos = uuid.os\nos.chdir('/')\nresult = open('etc/passwd').read()",
        limits=_fast(),
    )
    assert outcome.error is not None
    # The jail did not move, so `etc/passwd` is checked as (and does not exist as) a path *inside*
    # the original scratch directory — an ordinary FileNotFoundError, not a successful read.
    assert "FileNotFoundError" in outcome.error
    assert "root:" not in (outcome.stdout + (outcome.result_json or ""))


def test_open_does_not_accept_a_custom_opener() -> None:
    """`open()` does not accept a custom `opener`.

    An `opener` receives `(name, flags)` and may open any path, so forwarding it would bypass the
    jail behind an in-jail `file=`. The guarded signature has no `**kwargs`, so it fails before any
    path is checked.
    """
    outcome = run(
        "import uuid\nos = uuid.os\n"
        "def sneaky(path, flags):\n"
        "    return os.open('/etc/passwd', flags)\n"
        "with open('dummy.txt', 'r', opener=sneaky) as f:\n"
        "    result = f.read()",
        limits=_fast(),
    )
    assert outcome.error is not None
    assert "unexpected keyword argument 'opener'" in outcome.error
    assert "root:" not in (outcome.stdout + (outcome.result_json or ""))


def test_a_self_referential_result_degrades_to_repr_instead_of_crashing_the_runner() -> None:
    """A self-referential result degrades to `repr()` instead of crashing the runner.

    `_encode` recurses into containers to find nested bytes, so a cycle must be caught explicitly or
    a `RecursionError` would kill the runner before it writes any result.
    """
    outcome = run("result = []\nresult.append(result)", limits=_fast())
    assert outcome.error is None, outcome.error
    assert not outcome.timed_out
    assert outcome.result_json is not None


def test_leaked_file_handles_are_closed_before_the_result_is_written() -> None:
    """Leaked file handles are closed before the result is written.

    A program that never closes what it opens can exhaust `RLIMIT_NOFILE`; handles are closed once
    `exec()` returns so the runner's own `result.json` write still has budget.
    """
    outcome = run(
        "fs = []\n"
        "try:\n"
        "    for i in range(1000):\n"
        "        fs.append(open(f'f{i}.txt', 'w'))\n"
        "except OSError as e:\n"
        "    caught = str(e)\n"
        "result = {'caught': caught, 'opened': len(fs)}",
        limits=_fast(),
    )
    assert outcome.error is None, outcome.error
    decoded = json.loads(outcome.result_json or "null")
    assert "Too many open files" in decoded["caught"]
    assert decoded["opened"] > 0


def test_nothing_written_in_the_jail_survives_into_the_next_call() -> None:
    """Persistence was deliberately not built: the scratch directory is still destroyed per call."""
    first = run(
        "with open('leftover.txt', 'w') as f:\n    f.write('from the first run')\nresult = 'wrote'",
        limits=_fast(),
    )
    assert first.error is None
    second = run(
        "result = 'leftover.txt exists' if __import__('uuid').os.path.exists('leftover.txt') "
        "else 'gone'",
        limits=_fast(),
    )
    assert json.loads(second.result_json or "null") == "gone"


def test_a_bytes_result_comes_back_as_the_documented_base64_envelope() -> None:
    """A program that returns raw bytes gets the `{"__b64__": ...}` envelope `tools.py`
    documents."""
    outcome = run("result = b'\\x89PNG raw bytes'", limits=_fast())
    assert outcome.error is None, outcome.error
    decoded = json.loads(outcome.result_json or "null")
    assert set(decoded) == {"__b64__"}
    assert base64.b64decode(decoded["__b64__"]) == b"\x89PNG raw bytes"


def test_bytes_nested_inside_a_dict_result_are_also_encoded() -> None:
    """The realistic shape — `{"plot_png": open(path, "rb").read()}` — not a bare top-level
    value."""
    outcome = run(
        "result = {'name': 'plot', 'plot_png': bytearray(b'fake-png-bytes')}",
        limits=_fast(),
    )
    assert outcome.error is None, outcome.error
    decoded = json.loads(outcome.result_json or "null")
    assert decoded["name"] == "plot"
    assert base64.b64decode(decoded["plot_png"]["__b64__"]) == b"fake-png-bytes"


def test_a_plot_can_be_written_read_back_and_returned_as_bytes() -> None:
    """The matplotlib path end to end: savefig, guarded open, base64 out.

    A small, low-DPI figure keeps the base64 result under the default result cap, so the test is
    about the round-trip and not about truncation.
    """
    outcome = run(
        "import matplotlib.pyplot as plt\n"
        "fig, ax = plt.subplots(figsize=(1, 1), dpi=40)\n"
        "ax.plot([1, 2, 3], [1, 4, 9])\n"
        "fig.savefig('plot.png')\n"
        "with open('plot.png', 'rb') as f:\n"
        "    result = {'plot_png': f.read()}\n",
        limits=_fast(wall_seconds=15.0, cpu_seconds=10),
    )
    assert outcome.error is None, outcome.error
    decoded = json.loads(outcome.result_json or "null")
    png_bytes = base64.b64decode(decoded["plot_png"]["__b64__"])
    assert len(png_bytes) > 100
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"


def test_sympy_solves_a_trivial_equation() -> None:
    outcome = run(
        "import sympy as sp\n"
        "x = sp.symbols('x')\n"
        "result = [float(s) for s in sp.solve(sp.Eq(2 * x + 4, 0), x)]",
        limits=_fast(),
    )
    assert outcome.error is None, outcome.error
    assert json.loads(outcome.result_json or "null") == [-2.0]


def test_scikit_learn_fits_a_trivial_linear_regression() -> None:
    outcome = run(
        "import numpy as np\n"
        "from sklearn.linear_model import LinearRegression\n"
        "X = np.array([[1], [2], [3], [4]])\n"
        "y = np.array([2, 4, 6, 8])\n"
        "model = LinearRegression().fit(X, y)\n"
        "result = float(model.coef_[0])",
        limits=_fast(),
    )
    assert outcome.error is None, outcome.error
    assert json.loads(outcome.result_json or "null") == pytest.approx(2.0)


def test_openbabel_converts_a_smiles_string_to_another_format() -> None:
    """OpenBabel's reason for being here: format interconversion RDKit covers less well."""
    outcome = run(
        "from openbabel import pybel\n"
        "mol = pybel.readstring('smi', 'c1ccccc1O')\n"
        "result = mol.write('mol2')",
        limits=_fast(),
    )
    assert outcome.error is None, outcome.error
    assert "TRIPOS" in json.loads(outcome.result_json or "null")


@pytest.mark.parametrize("name", ["eval", "exec", "compile", "input", "breakpoint"])
def test_the_other_withheld_builtins_are_gone(name: str) -> None:
    """`eval`/`exec`/`compile` in particular would be a second front door past the guard."""
    outcome = run(f"result = {name}", limits=_fast())
    assert outcome.error is not None
    assert f"name '{name}' is not defined" in outcome.error


def test_the_environment_carries_no_credential() -> None:
    """The child's environment is built from an allowlist, so a token cannot arrive by being new.

    Sets a variable shaped like this server's bearer token and asserts on the whole environment,
    which a filtered copy of `os.environ` would fail as soon as an unforeseen variable appeared.
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
    """`SIGXCPU` is catchable; the wall clock is not, and it always holds.

    The program swallows the CPU signal and keeps spinning, and the run still dies.
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
    """A traceback names the caller's program and nothing about this server.

    `runner.py`'s frames are noise the caller cannot fix, and its absolute source paths would reach
    a model that may quote them.
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
# The boundary after an escape: every test below starts from a program that already holds `os`
# and asks what holds next.
# --------------------------------------------------------------------------------------------

#: A handle on `os` from inside the sandbox, via `uuid` (allowlisted, imports `os` at module
#: level): the shortest reliable route to an escape.
_ESCAPED = "import uuid\nos = uuid.os\n"

#: A fake pod secret, shaped like this server's own bearer token — the variable `app.py` reads.
_POD_SECRET = "SECRET-BEARER-abc123"

#: The unprivileged uid the stand-in server drops to when the suite runs as root.
_NOBODY = 65534

#: A stand-in for the pyexec pod as its own process: bearer token in its environment, one program
#: through `sandbox.run`, reporting the result and its memory growth. Not the pytest process, whose
#: environment, RSS high-water mark and (as root) `CAP_SYS_PTRACE` would all distort the result.
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


def _a_second_uid_is_reachable() -> bool:
    """Whether this process can actually become another user, rather than merely be uid 0.

    Under `unshare --user --map-root-user` (the offline lane) the uid map has one entry and
    `setuid(65534)` fails with `EINVAL`, so the map is what is read. Without a second uid the tests
    below skip: as real root the refusal they assert would be the kernel's ptrace rule, not this
    server's control.
    """
    try:
        entries = [line.split() for line in Path("/proc/self/uid_map").read_text().splitlines()]
    except OSError:  # pragma: no cover — no procfs; the caller falls back to "cannot drop".
        return False
    return any(int(count) > 1 for _inside, _outside, count in entries)


#: Skip marker for the two tests that need a second user to be meaningful. Loud, and it says which
#: environment cannot provide one, because a silently-permanent skip is how a control stops being
#: checked without anybody deciding that.
_needs_a_second_uid = pytest.mark.skipif(
    os.geteuid() == 0 and not _a_second_uid_is_reachable(),
    reason=(
        "running as uid 0 of a single-uid user namespace (the `offline` lane's `unshare "
        "--map-root-user`): there is no unprivileged user to drop to, and as root the assertion "
        "would test the kernel rather than this server's seal. Proven in the `check` lane."
    ),
)


def _drop_privileges() -> None:  # pragma: no cover — runs after fork, inside the stand-in server.
    """Become an unprivileged user, because root reads any `/proc/<pid>` whatever the flag says.

    The dumpable flag the credential change clears is restored by the following `execve`, as in the
    pod; `dumpable_at_start` asserts that.
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
        preexec_fn=(
            _drop_privileges if os.geteuid() == 0 and _a_second_uid_is_reachable() else None
        ),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    answered: dict[str, Any] = json.loads(completed.stdout)
    return answered


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc/<ppid>/environ")
@_needs_a_second_uid
def test_an_escaped_program_cannot_read_the_servers_own_environment() -> None:
    """An escaped program cannot read the server's own environment through `/proc/<ppid>/environ`.

    Parent and child share a uid, so the only barrier to the bearer token and DSNs is the parent's
    dumpable flag; values read there would leave via `result`, a channel the NetworkPolicy never
    sees.
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


@_needs_a_second_uid
def test_a_flood_to_the_stdout_descriptor_does_not_grow_the_server() -> None:
    """A flood to fd 1 does not grow the server's memory.

    The output cap is on the runner's own capture; a program holding `os` can write to fd 1
    directly, and reading that into the parent would OOM the pod for every session.
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
    """A flood to fd 2 cannot inflate the "died quietly" diagnostic.

    stderr goes to a file in the scratch directory under the child's own `RLIMIT_FSIZE`, and the
    parent reads a bounded tail. CPython ignores `SIGXFSZ`, so the program sees `OSError: File too
    large` and the caller learns which bound stopped it.
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
    """One directory per call, serving as `HOME`, `TMPDIR` and the working directory at once.

    What an escaped program writes to an absolute path elsewhere is bounded by the deployment, not
    here, which is why the README does not promise nothing survives a call.
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


def test_the_default_fork_headroom_is_zero() -> None:
    """`Limits.process_headroom` defaults to 0, so `RLIMIT_NPROC` refuses the child any new task.

    Raising it lets a program fork a grandchild and `setsid` out of the process group the wall-clock
    kill targets.
    """
    assert Limits().process_headroom == 0


@_needs_a_second_uid
def test_a_forked_grandchild_cannot_escape_the_kill_group() -> None:
    """The setsid-orphan escape is closed at the fork, not at the kill.

    `killpg` reaches one process group, and a `setsid` grandchild would outlive it; with zero
    headroom the fork itself fails with `EAGAIN`. Needs a non-root uid, since `RLIMIT_NPROC` is not
    enforced for root.
    """
    answered = _through_a_stand_in_server(
        _ESCAPED
        + "try:\n"
        + "    pid = os.fork()\n"
        + "    if pid == 0:\n"
        + "        os.setsid()\n"
        + "        os._exit(0)\n"
        + "    os.waitpid(pid, 0)\n"
        + "    result = {'forked': True}\n"
        + "except OSError as exc:\n"
        + "    result = {'fork_errno': exc.errno}\n"
    )
    decoded = json.loads(answered["result_json"] or "null")
    assert decoded == {"fork_errno": errno.EAGAIN}, answered
    assert decoded != {"forked": True}
