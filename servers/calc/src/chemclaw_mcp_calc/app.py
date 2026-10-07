"""The `calc` server's FastAPI app.

    uvicorn chemclaw_mcp_calc.app:app --host 127.0.0.1 --port 8860

Bearer auth via `CHEMCLAW_CALC_TOKEN`, enforced even on loopback and failing closed when unset.
`on_start` resolves the calculator versions once (possibly an `xtb --version` subprocess), so the
first request does not pay it on the event loop.
"""

from __future__ import annotations

from fastapi import FastAPI
from mcp_server_kit import Dataset, connector_app

from chemclaw_mcp_calc.engine import xtb_cli
from chemclaw_mcp_calc.engine.pka import calc_version
from chemclaw_mcp_calc.engine.xtb_spec import resolve_backend
from chemclaw_mcp_calc.tools import resolve_calculator_versions, server


def _readiness() -> list[Dataset]:
    """Prove this pod can derive a usable `calc_version`, which every tool here needs first.

    No corpus, so the list is empty. Stricter than `on_start`: a pod that cannot name its calculator
    must not take traffic. It also refuses `CHEMCLAW_XTB_ENGINE=xtb` without the binary, because the
    version string would still derive (as `xtb-absent`) and key every cached and calibrated result
    to
    a program the image lacks.
    """
    calc_version()
    backend = resolve_backend()
    if backend == "xtb" and not xtb_cli.is_available():
        raise RuntimeError(
            "CHEMCLAW_XTB_ENGINE selects the xtb binary and this image has none on PATH, so every "
            "result would be keyed `xtb-absent` in Chemclaw3's cache and calibration ledger. "
            "Install xtb in the image or unset the variable to fall back to tblite."
        )
    return []


app: FastAPI = connector_app(
    server,
    name="calc",
    token_env="CHEMCLAW_CALC_TOKEN",  # noqa: S106 - an environment variable's *name*, never a credential
    on_start=resolve_calculator_versions,
    readiness=_readiness,
)
