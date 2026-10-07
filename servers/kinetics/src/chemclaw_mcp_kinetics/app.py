"""The `kinetics` server's FastAPI app.

    uvicorn chemclaw_mcp_kinetics.app:app --host 127.0.0.1 --port 8852

`mcp_server_kit.connector_app` owns the shape. Bearer auth via `CHEMCLAW_KINETICS_TOKEN` is
enforced even on loopback; unset, every request fails closed with 401. Readiness runs the
arithmetic against relations it does not contain (see `engine/selftest.py`).
"""

from fastapi import FastAPI
from mcp_server_kit import Dataset, connector_app

from chemclaw_mcp_kinetics.engine import selftest
from chemclaw_mcp_kinetics.tools import server


def _readiness() -> list[Dataset]:
    """Recompute the relations, and name the formulas this pod serves."""
    return selftest.verify()


app: FastAPI = connector_app(
    server,
    name="kinetics",
    token_env="CHEMCLAW_KINETICS_TOKEN",  # noqa: S106 - a variable's *name*, never a credential
    readiness=_readiness,
)
