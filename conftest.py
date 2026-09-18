"""The suite runs with the egress guard armed. That is the point, not a precaution.

Importing `mcp_server_kit` arms it already; this fixture asserts the state rather than establishing
it, so a change that quietly disarms the guard fails here instead of in a deployment. The one place
that legitimately turns it off is `packages/mcp_server_kit/tests/test_egress.py`, which restores it.

The consequence worth naming: a test that only passes because it reached the internet fails. So the
vendored datasets are *proven* sufficient rather than assumed to be — which is the property the
whole no-egress design is trying to buy.
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

    Loaded from that file by path rather than transcribed here, and rather than imported by name:
    a reporter matching a phrase two files claim to share is the one-fact-declared-twice defect one
    layer up, and it fails by reporting nothing at all — which is the exact failure this reporter
    exists to prevent. By path rather than by `import` because this suite runs with
    `--import-mode=importlib` and no `tests/__init__.py`, so whether `tests.test_consumer_agreement`
    resolves depends on how pytest was invoked (`python -m pytest` puts the working directory on
    `sys.path`; `uv run pytest` does not). A rename of that module raises here, loudly, in every
    run — which is the right answer, because a renamed module is a retired check.
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

    A skipped report's `longrepr` is `(path, lineno, "Skipped: <reason>")`. Read rather than
    reconstructed from the marker, because a `pytest.skip(...)` call inside a test body and a
    `@pytest.mark.skipif` reason arrive the same way here and differently everywhere else.
    """
    longrepr = getattr(report, "longrepr", None)
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2]).removeprefix("Skipped: ").strip()
    return str(longrepr).strip()


def _report_every_skip(terminalreporter: pytest.TerminalReporter) -> None:
    """Name every skip and its reason, so a green line says what it did not look at.

    This repository printed a bare `18 skipped`, and a reviewer of it had to re-run the whole suite
    with `-rs` to find out which checks a green run was not evidence about — ten servers' wheel
    builds, every `xtb`- and `crest`-computed answer in `servers/calc`, and the torch degradation
    arm. A skip is not a pass, and a count with no names is not a report; the sibling `Chemclaw3`
    prints a named epilogue for exactly this reason and this tree had one only for the
    cross-repository check below.

    **Every number here comes from the run that prints it.** Nothing in this function knows how many
    skips to expect, which is the rule `D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose`
    reaches one repository over — a figure written into prose describes the suite on the day
    somebody counted, and the skip counts in this tree move with what is installed on the box.

    Grouped by the reason pytest recorded, not by a marker this function matches: a marker list
    would go stale against a new skip in exactly the way the bare count did, and the failure mode
    would be silence about the newest thing.
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

    A skip is not a pass. `_report_every_skip` names all of them, counted from this run; the banner
    below stays beside it for the one skip that has a *remedy* to print.
    `tests/test_consumer_agreement.py` is the only check in this tree that reads the repository
    consuming this fleet's manifests and its recorded `calc` surface, and "set CHEMCLAW3_REPO" is
    something the generic epilogue cannot know to say. Reported rather than left in the `-rs` output
    nobody passes, which is the shape `tests/test_backlog_register.py` already uses for the rows it
    cannot open.
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
