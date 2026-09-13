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
"""

from __future__ import annotations

import pytest
from chemclaw_mcp_calc import app as app_module
from chemclaw_mcp_calc.engine import xtb_cli
from chemclaw_mcp_calc.engine.config import settings


def test_an_explicit_xtb_backend_with_no_binary_refuses_traffic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The configuration that derived a version naming a program the image does not carry."""
    monkeypatch.setattr(settings, "xtb_engine", "xtb")
    monkeypatch.setattr(xtb_cli, "is_available", lambda: False)
    with pytest.raises(RuntimeError) as unready:
        app_module._readiness()
    assert "xtb" in str(unready.value)


def test_an_explicit_xtb_backend_with_the_binary_present_is_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other direction, so the test above is about the binary and not about the setting.

    Forced rather than skipped where no binary exists, for the reason
    `test_reactivity_panel.py` gives for the same trick: the assertion is about what the code would
    do on a deployment that has one.
    """
    monkeypatch.setattr(settings, "xtb_engine", "xtb")
    monkeypatch.setattr(xtb_cli, "is_available", lambda: True)
    assert app_module._readiness() == []


def test_the_default_auto_backend_is_ready_on_an_image_with_no_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`auto` falls back to tblite, which is the whole reason `binary_version` may answer `absent`.

    This is the arm that would break if the refusal above were written against the *binary* rather
    than against the *resolved backend* — every pod in this fleet's dev lane would go unready.
    """
    monkeypatch.setattr(settings, "xtb_engine", "auto")
    monkeypatch.setattr(xtb_cli, "is_available", lambda: False)
    assert app_module._readiness() == []
