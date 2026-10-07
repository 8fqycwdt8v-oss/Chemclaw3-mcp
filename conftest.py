"""The suite runs with the egress guard armed.

Importing `mcp_server_kit` arms it; this fixture asserts the state so a change that disarms the
guard fails here. A test that only passes by reaching the internet fails, which proves the vendored
datasets sufficient. Only `packages/mcp_server_kit/tests/test_egress.py` turns it off, and restores
it.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from mcp_server_kit import egress


@pytest.fixture(autouse=True)
def egress_guard_is_armed() -> Iterator[None]:
    """Fail any test that runs without the guard, and leave it armed for the next one."""
    if not egress.armed():
        egress.arm()
    yield
    if not egress.armed():
        egress.arm()


def _consumer_skip_marker() -> str:
    """The marker `tests/test_consumer_agreement.py` puts at the head of its skip reasons.

    Loaded from that file by path so the phrase is declared once, and by path rather than `import`
    because `--import-mode=importlib` makes the import depend on how pytest was invoked. A rename
    raises here, loudly.
    """
    path = Path(__file__).resolve().parent / "tests" / "test_consumer_agreement.py"
    spec = importlib.util.spec_from_file_location("_consumer_agreement_marker", path)
    if spec is None or spec.loader is None:  # pragma: no cover - unreadable source
        raise RuntimeError(f"{path} could not be read, so cross-repository skips go unreported")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    marker: str = module.CONSUMER_SKIP
    return marker


def _skip_reason(report: pytest.TestReport) -> str:
    """The reason pytest recorded for one skip, as a plain sentence.

    A skipped report's `longrepr` is `(path, lineno, "Skipped: <reason>")`; both `pytest.skip()` and
    `skipif` arrive that way.
    """
    longrepr = getattr(report, "longrepr", None)
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2]).removeprefix("Skipped: ").strip()
    return str(longrepr).strip()


def _report_every_skip(terminalreporter: pytest.TerminalReporter) -> None:
    """Name every skip and its reason, so a green line says what it did not look at.

    Counts come from this run, grouped by the recorded reason rather than a marker list, so a new
    skip is reported rather than silently missed.
    """
    skipped = terminalreporter.stats.get("skipped", [])
    if not skipped:
        return
    grouped: dict[str, list[str]] = {}
    for report in skipped:
        grouped.setdefault(_skip_reason(report), []).append(report.nodeid)
    terminalreporter.write_sep(
        "=", f"{len(skipped)} skipped — what this run is NOT evidence about", yellow=True
    )
    for reason, nodeids in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0])):
        files = sorted({nodeid.split("::", 1)[0] for nodeid in nodeids})
        terminalreporter.write_line(f"  {len(nodeids):>3}  {reason}")
        terminalreporter.write_line(f"       in {', '.join(files)}")
    terminalreporter.write_line(
        "Each line is a check that did not run. The rest of this run says nothing about it."
    )


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """Say, at the end of the run, what did not run and why.

    `_report_every_skip` names every skip; the banner below adds the remedy ("set CHEMCLAW3_REPO")
    for the one skip that has one — `tests/test_consumer_agreement.py`, the only check reading the
    consuming repository.
    """
    _report_every_skip(terminalreporter)
    marker = _consumer_skip_marker()
    skipped = [
        report
        for report in terminalreporter.stats.get("skipped", [])
        if marker in str(getattr(report, "longrepr", ""))
    ]
    if skipped:
        terminalreporter.write_sep(
            "!",
            f"{len(skipped)} cross-repository check(s) did NOT run: no Chemclaw3 checkout. "
            "This run is not evidence that the consumer still agrees with the surface this tree "
            "declares. Set CHEMCLAW3_REPO, or clone it beside this one.",
            yellow=True,
        )
