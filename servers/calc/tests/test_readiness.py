"""`/healthz` refuses when the resolved backend names a program this image lacks.

`calc_version` keys Chemclaw3's cache and ledger. An explicit `CHEMCLAW_XTB_ENGINE=xtb` resolves
to the binary without checking it exists, and `binary_version()` answers `"absent"`, so without
this gate the pod would write rows under a version naming a missing program.

Binary-only tasks (`atomic`, `surface`) are not a readiness matter: a pod serving most tools is
serving, so those tools refuse at the point of asking (`engine/identity.py`).

Driven through `/healthz` over ASGI, without the lifespan, so the status code, memo, redaction
and single-flight lock are included.
"""

from __future__ import annotations

import httpx
import pytest
from chemclaw_mcp_calc import app as app_module
from chemclaw_mcp_calc.engine import xtb_cli
from chemclaw_mcp_calc.engine.config import settings


@pytest.fixture(autouse=True)
def no_readiness_memo(monkeypatch: pytest.MonkeyPatch) -> None:
    """`connector_app` believes a failure for five seconds; these tests must not inherit one."""
    monkeypatch.setattr("mcp_server_kit.app.READINESS_FAILURE_TTL_SECONDS", 0.0)


async def _probe() -> httpx.Response:
    """GET `/healthz` on the real app, over ASGI and without running its lifespan."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_module.app), base_url="http://calc.test"
    ) as client:
        return await client.get("/healthz")


async def test_an_explicit_xtb_backend_with_no_binary_refuses_traffic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The configuration that derived a version naming a program the image does not carry.

    A whole-pod fault rather than a per-tool one, which is why it belongs on this route: with
    `CHEMCLAW_XTB_ENGINE=xtb` every tool on the server keys `xtb-absent`, not two of them.
    """
    monkeypatch.setattr(settings, "xtb_engine", "xtb")
    monkeypatch.setattr(xtb_cli, "is_available", lambda: False)
    response = await _probe()
    assert response.status_code == 503
    assert "xtb" in response.json()["reason"]


async def test_an_explicit_xtb_backend_with_the_binary_present_is_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit `xtb` backend with the binary present is ready.

    The other direction, forced rather than skipped, so the test above is about the binary.
    """
    monkeypatch.setattr(settings, "xtb_engine", "xtb")
    monkeypatch.setattr(xtb_cli, "is_available", lambda: True)
    assert (await _probe()).status_code == 200


async def test_the_default_auto_backend_is_ready_on_an_image_with_no_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`auto` falls back to tblite, so a pod with no binary is ready.

    This breaks if the refusal is written against the binary rather than the resolved backend. The
    binary-only tools are closed at the key, not at readiness; see the test below.
    """
    monkeypatch.setattr(settings, "xtb_engine", "auto")
    monkeypatch.setattr(xtb_cli, "is_available", lambda: False)
    assert (await _probe()).status_code == 200


async def test_the_two_binary_only_tools_do_not_key_on_a_ready_pod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ready, and no key naming a program this image lacks.

    Together, because each half alone is satisfied by a wrong fix.
    """
    monkeypatch.setattr(settings, "xtb_engine", "auto")
    monkeypatch.setattr(xtb_cli, "is_available", lambda: False)
    assert (await _probe()).status_code == 200

    from chemclaw_mcp_calc.engine.identity import calculation_identity

    for tool in ("compute_atomic_descriptors", "compute_surface_potential"):
        with pytest.raises(ValueError) as refused:
            calculation_identity(tool, {"smiles": "CCO"})
        assert "xtb" in str(refused.value), tool
