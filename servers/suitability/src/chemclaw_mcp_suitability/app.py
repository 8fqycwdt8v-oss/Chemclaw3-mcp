"""The `suitability` server's FastAPI app.

    uvicorn chemclaw_mcp_suitability.app:app --host 127.0.0.1 --port 8892

`mcp_server_kit.connector_app` owns the shape. Bearer auth on `CHEMCLAW_SUITABILITY_TOKEN`, as
`connector.yaml` declares, enforced even on loopback; an unset token fails closed with 401. With no
corpus, readiness runs the arithmetic: USP's constants over-determine each other, so a transposed
digit breaks their agreement (`engine/selftest.py`).
"""

from fastapi import FastAPI
from mcp_server_kit import Dataset, connector_app

from chemclaw_mcp_suitability.engine import selftest
from chemclaw_mcp_suitability.tools import server


def _readiness() -> list[Dataset]:
    """Recompute the constants against each other, and name what this pod serves."""
    return selftest.verify()


app: FastAPI = connector_app(
    server,
    name="suitability",
    token_env="CHEMCLAW_SUITABILITY_TOKEN",  # noqa: S106 - a variable's *name*, never a credential
    readiness=_readiness,
)
