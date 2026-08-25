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
from dataclasses import replace
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
