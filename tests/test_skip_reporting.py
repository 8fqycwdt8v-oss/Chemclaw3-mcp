"""The run says what it did not look at, and the saying is measured rather than believed.

A bare `18 skipped` is a count with no names: a reviewer of this repository had to re-run the whole
suite with `-rs` to learn that a green line was not evidence about ten servers' wheel builds, every
`xtb`- and `crest`-computed answer in `servers/calc`, or the torch degradation arm. The epilogue
`conftest.py::_report_every_skip` prints closes that, and this file is what keeps it closed —
including the property that matters most about it: **the count comes from the run.** A reporter that
prints a literal is the defect it was written to fix, wearing the fix's clothes.

Driven against a real pytest process rather than a stubbed `TerminalReporter`, because the thing
being checked is a hook's behaviour inside a session: `-p conftest` loads this repository's root
`conftest.py` as a plugin over a throw-away test file, which is the only way to skip a controlled
number of tests without writing them into the tree.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_SKIPPING_MODULE = """import pytest


@pytest.mark.parametrize("n", range(3))
def test_first_reason(n: int) -> None:
    pytest.skip("a fixture this run does not have")


def test_second_reason() -> None:
    pytest.skip("a binary this box does not carry")


def test_that_passes() -> None:
    assert True
"""


def _run(target: Path) -> str:
    """Run a real pytest session over `target` with this repository's root conftest loaded."""
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "conftest", str(target)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout


def test_the_epilogue_names_every_skip_and_counts_them_from_the_run(tmp_path: Path) -> None:
    """Four skips under two reasons: both reasons named, both counts right, total right.

    The two counts are what make this more than a smoke test. A reporter that grouped by a marker
    list, or that printed `len(stats)` once without splitting, passes a single-reason probe.
    """
    target = tmp_path / "test_skips.py"
    target.write_text(_SKIPPING_MODULE, encoding="utf-8")
    output = _run(target)

    assert "4 skipped — what this run is NOT evidence about" in output, output
    assert "3  a fixture this run does not have" in output, output
    assert "1  a binary this box does not carry" in output, output
    assert "test_skips.py" in output, output
    assert "1 passed, 4 skipped" in output, output


def test_a_run_with_no_skips_prints_no_epilogue(tmp_path: Path) -> None:
    """The other direction: a section that appears when there is nothing to say is noise.

    This is the half a one-way check misses — an epilogue printed unconditionally would satisfy
    every assertion above while telling a clean run it is missing something.
    """
    target = tmp_path / "test_clean.py"
    target.write_text("def test_that_passes() -> None:\n    assert True\n", encoding="utf-8")
    output = _run(target)

    assert "what this run is NOT evidence about" not in output, output
    assert "1 passed" in output, output
