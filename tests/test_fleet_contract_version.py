"""Every server reports, on `/healthz`, the `contract_version` its manifest carries, if any.

The version lives in one place, the packaged manifest (`chemclaw_contracts`); `connector_app` reads
it from there, so a server cannot report a version its manifest does not declare. This holds the
whole served fleet to that. The apps run in a child interpreter so importing eleven servers' engines
leaves this process's heap as it was (`test_session_ceiling` measures RSS growth in it).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from chemclaw_contracts import (
    CONTRACT_VERSION_PATTERN,
    contract_version,
    declared_contract_version,
    manifest_path,
)
from mcp_server_kit.testing import load_manifest

ROOT = Path(__file__).resolve().parents[1]

#: Import each server's app, ask its `/healthz` without a socket, print `{name: [status, version]}`.
_PROBE = """
import importlib, json, sys
from fastapi.testclient import TestClient

answers = {}
for name in sys.argv[1:]:
    app = importlib.import_module(f"chemclaw_mcp_{name}.app").app
    body = TestClient(app).get("/healthz")
    answers[name] = [body.status_code, body.json().get("contract_version")]
json.dump(answers, sys.stdout)
"""


def _servers() -> list[str]:
    """Every server directory, by name."""
    return sorted(p.name for p in (ROOT / "servers").iterdir() if (p / "connector.yaml").exists())


@pytest.fixture(scope="module")
def healthz() -> dict[str, list[object]]:
    """Each server's `[status, contract_version]` from its own `/healthz`."""
    run = subprocess.run(
        [sys.executable, "-c", _PROBE, *_servers()],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=300,
    )
    assert run.returncode == 0, run.stderr[-3000:]
    answers: dict[str, list[object]] = json.loads(run.stdout)
    return answers


@pytest.mark.parametrize("name", _servers())
def test_healthz_reports_the_manifest_s_contract_version(
    name: str, healthz: dict[str, list[object]]
) -> None:
    """The version on `/healthz` is the manifest's, ready or not, for every server."""
    status, reported = healthz[name]
    assert status in (200, 503), f"{name}: /healthz answered {status}"
    declared = load_manifest(manifest_path(name)).contract_version
    assert reported == declared, (
        f"{name}: /healthz reports {reported!r} and the manifest declares {declared!r}"
    )


@pytest.mark.parametrize("name", _servers())
def test_a_declared_contract_version_is_semver_and_read_the_same_three_ways(name: str) -> None:
    """The helper, the file read and the stand-in model agree, and a present value is semver.

    Presence is not required: the consumer reads a missing value as unknown and never refuses
    on it. When a manifest has one, every route to it must give one answer.
    """
    path = manifest_path(name)
    declared = declared_contract_version(path)
    assert contract_version(name) == declared == load_manifest(path).contract_version
    if declared is not None:
        assert re.fullmatch(CONTRACT_VERSION_PATTERN, declared)


def test_every_manifest_declares_a_contract_version() -> None:
    """A manifest without a `MAJOR.MINOR.PATCH` version is a surface nobody can version."""
    for name in _servers():
        assert contract_version(name) is not None, f"{name}/connector.yaml declares none"
