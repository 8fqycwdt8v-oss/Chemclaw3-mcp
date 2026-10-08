"""Every server reports, on `/healthz`, the `contract_version` its manifest carries.

The version lives in one place, the packaged manifest (`chemclaw_contracts`); `connector_app` reads
it from there, so a server cannot report a version its manifest does not declare. This holds the
whole served fleet to that, and the manifests to carrying one.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from chemclaw_contracts import manifest_path
from fastapi.testclient import TestClient
from mcp_server_kit.testing import load_manifest

ROOT = Path(__file__).resolve().parents[1]


def _servers() -> list[str]:
    """Every server directory, by name."""
    return sorted(p.name for p in (ROOT / "servers").iterdir() if (p / "connector.yaml").exists())


@pytest.mark.parametrize("name", _servers())
def test_healthz_reports_the_manifest_s_contract_version(name: str) -> None:
    """The version on `/healthz` is the manifest's, ready or not, for every server."""
    app = importlib.import_module(f"chemclaw_mcp_{name}.app").app
    response = TestClient(app).get("/healthz")
    assert response.status_code in (200, 503), response.text
    declared = load_manifest(manifest_path(name)).contract_version
    assert response.json().get("contract_version") == declared, (
        f"{name}: /healthz reports {response.json().get('contract_version')!r} and the manifest "
        f"declares {declared!r}"
    )
