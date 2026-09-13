"""`/healthz` here proved a `calc_version` could be *derived*, which is weaker than it reads.

The probe calls `calc_version()`, and that string is the primary key of Chemclaw3's calculation
cache and its calibration ledger — so the check is right to exist and its docstring is right that a
pod which cannot name its calculator should not be sent a calculation. What it missed is that the
string can be derived and still name a program this image does not have.

`resolve_backend()` honours an explicit `CHEMCLAW_XTB_ENGINE=xtb` without asking whether the binary
exists, and `xtb_cli.binary_version()` answers `"absent"` rather than raising — deliberately, on the
argument that "`resolve_backend()` will therefore never select `xtb`", which holds under `auto` and
not under the explicit setting. Measured on an image with no `xtb` on `PATH`:

    $ CHEMCLAW_XTB_ENGINE=xtb ... GET /healthz
    200 {"status":"ok","server":"calc",...}
    calc_version() -> '...opt-GFN2-xTB+xtb+xtb-absent/tblite-0.7.0/rdkit-2026.3.5/h2'

So the pod took traffic and wrote ledger rows under a version naming a program that was not there —
and those rows become unreachable the day the binary arrives and the key moves.

**That gate closed the configuration and not the key**, which is the correction this file now
carries.
`xtb_spec._FIXED_BACKEND` pins the `atomic` and `surface` tasks to the binary *regardless* of
configuration, and the gate tests `resolve_backend()` — which under the shipped `auto` default
answers `tblite`. Driven on this checkout: `/healthz` **200** and `calculation_key` answering
`xtb.atomic@GFN2-xTB+xtb+xtb-absent/...` for 2 of 17 tools. The answer is *not* a wider readiness
gate: a pod that can serve fifteen of seventeen tools is serving, and refusing it would take every
dev pod in this fleet out of rotation for a binary no dev image carries. The answer is that those
two
tools refuse at the point of asking exactly as they already refused at the point of computing —
`engine/identity.py`, and `servers/calc/tests/test_calculation_key.py::
test_the_tools_that_need_a_binary_refuse_rather_than_key`.

**Driven through `/healthz` rather than through `_readiness()`**, which is what these tests did and
is weaker than it reads: the probe function is not what a kubelet calls, so a direct call misses the
status code, the five-second memo, the redaction and the single-flight lock `connector_app` wraps it
in. The app is driven over ASGI without its lifespan, because `/healthz` needs no session manager
and
`StreamableHTTPSessionManager.run()` may be called only once per instance.
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
    """The other direction, so the test above is about the binary and not about the setting.

    Forced rather than skipped where no binary exists, for the reason
    `test_reactivity_panel.py` gives for the same trick: the assertion is about what the code would
    do on a deployment that has one.
    """
    monkeypatch.setattr(settings, "xtb_engine", "xtb")
    monkeypatch.setattr(xtb_cli, "is_available", lambda: True)
    assert (await _probe()).status_code == 200


async def test_the_default_auto_backend_is_ready_on_an_image_with_no_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`auto` falls back to tblite, which is the whole reason `binary_version` may answer `absent`.

    This is the arm that would break if the refusal above were written against the *binary* rather
    than against the *resolved backend* — every pod in this fleet's dev lane would go unready.

    **And it is the arm a reviewer read as pinning a hole open**, which is worth stating because it
    was a reasonable reading of a file that did not say otherwise: this configuration really did
    leave `calculation_key` minting `xtb.atomic@...xtb-absent/...`. What was wrong was not this
    assertion but the conclusion that readiness is where the key had to be closed. The key is closed
    where the calculation refuses, and the pod stays ready — see the module docstring, and
    `test_the_two_binary_only_tools_do_not_key_on_a_ready_pod` below.
    """
    monkeypatch.setattr(settings, "xtb_engine", "auto")
    monkeypatch.setattr(xtb_cli, "is_available", lambda: False)
    assert (await _probe()).status_code == 200


async def test_the_two_binary_only_tools_do_not_key_on_a_ready_pod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the arm above: ready, and no key naming a program this image lacks.

    Asserted together, in one test, because separately they are each satisfiable by the wrong fix —
    a wider readiness gate satisfies the second and breaks the first, and the shipped state
    satisfied
    the first while leaving the second false.
    """
    monkeypatch.setattr(settings, "xtb_engine", "auto")
    monkeypatch.setattr(xtb_cli, "is_available", lambda: False)
    assert (await _probe()).status_code == 200

    from chemclaw_mcp_calc.engine.identity import calculation_identity

    for tool in ("compute_atomic_descriptors", "compute_surface_potential"):
        with pytest.raises(ValueError) as refused:
            calculation_identity(tool, {"smiles": "CCO"})
        assert "xtb" in str(refused.value), tool
