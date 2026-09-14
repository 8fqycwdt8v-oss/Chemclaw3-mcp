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


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """Say, at the end of the run, that the cross-repository check did not run, and why.

    A skip is not a pass. `tests/test_consumer_agreement.py` is the only check in this tree that
    reads the repository consuming this fleet's manifests and its recorded `calc` surface, and it
    can only run where that checkout exists — so a green run without it is evidence about strictly
    less. Reported rather than left in the `-rs` output nobody passes, which is the shape
    `tests/test_backlog_register.py` already uses for the rows it cannot open.
    """
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
