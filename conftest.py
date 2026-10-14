"""The suite runs with the egress guard armed, and renders the fleet chart for the deploy checks.

Importing `mcp_server_kit` arms it; this fixture asserts the state so a change that disarms the
guard fails here. A test that only passes by reaching the internet fails, which proves the vendored
datasets sufficient. Only `packages/mcp_server_kit/tests/test_egress.py` turns it off, and restores
it.

The `fleet_render` fixture runs `helm template` over `deploy/helm/chemclaw-fleet`, which is the one
description of how a server is deployed; every deploy check reads that output, never the templates.
Without `helm` it skips and says so, unless `CHEMCLAW_REQUIRE_HELM` is set (CI sets it), where the
absence is a failure.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp_server_kit import egress

CHART = Path(__file__).resolve().parent / "deploy" / "helm" / "chemclaw-fleet"
REQUIRE_HELM_ENV = "CHEMCLAW_REQUIRE_HELM"
HELM_TIMEOUT_SECONDS = 120


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


def _helm() -> str:
    """The `helm` binary, or a skip (a failure where `CHEMCLAW_REQUIRE_HELM` is set) naming it."""
    found = shutil.which("helm")
    if found:
        return found
    message = (
        "helm is not installed, so every check on the rendered fleet chart did not run "
        f"(CI installs it and sets {REQUIRE_HELM_ENV}=1)"
    )
    if os.environ.get(REQUIRE_HELM_ENV):
        pytest.fail(message)
    pytest.skip(message)


@pytest.fixture(scope="session")
def fleet_render() -> Callable[..., list[dict[str, Any]]]:
    """`render(*helm_args)`: every object `helm template` emits for the fleet chart, cached.

    Rendered into the `chemclaw` namespace with the chart's own defaults plus `helm_args` (for
    example `"--set", "autoscaling.mode=keda"`). A render that fails fails the test with helm's
    message, since a chart that does not render is the finding.
    """
    cache: dict[tuple[str, ...], list[dict[str, Any]]] = {}

    def render(*helm_args: str) -> list[dict[str, Any]]:
        if helm_args not in cache:
            command = [_helm(), "template", "chemclaw-fleet", str(CHART), "--namespace", "chemclaw"]
            done = subprocess.run(  # noqa: S603 - the binary and every argument are the test's own
                [*command, *helm_args],
                capture_output=True,
                text=True,
                check=False,
                timeout=HELM_TIMEOUT_SECONDS,
            )
            if done.returncode != 0:
                pytest.fail(f"helm template {' '.join(helm_args)} failed:\n{done.stderr}")
            cache[helm_args] = [doc for doc in yaml.safe_load_all(done.stdout) if doc]
        return cache[helm_args]

    return render


@pytest.fixture(scope="session")
def fleet_deployment(
    fleet_render: Callable[..., list[dict[str, Any]]],
) -> Callable[[str], dict[str, Any]]:
    """`deployment(server)`: the rendered Deployment `chemclaw-mcp-<server>`, for its own tests."""

    def deployment(server: str) -> dict[str, Any]:
        wanted = f"chemclaw-mcp-{server}"
        for doc in fleet_render():
            if doc["kind"] == "Deployment" and doc["metadata"]["name"] == wanted:
                return doc
        pytest.fail(f"the fleet chart renders no Deployment named {wanted}")

    return deployment
