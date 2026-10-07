"""The `unitops` server's FastAPI app.

    uvicorn chemclaw_mcp_unitops.app:app --host 127.0.0.1 --port 8853

`mcp_server_kit.connector_app` owns the shape. Bearer auth on `CHEMCLAW_UNITOPS_TOKEN`, as
`connector.yaml` declares, enforced even on loopback; an unset token fails closed with 401.
Readiness checks every correlation against a relation it does not itself contain
(`engine/selftest.py`).
"""

from fastapi import FastAPI
from mcp_server_kit import Dataset, connector_app

from chemclaw_mcp_unitops.engine import selftest
from chemclaw_mcp_unitops.tools import server


def _readiness() -> list[Dataset]:
    """Recompute the relations, and name the correlations this pod serves."""
    return selftest.verify()


app: FastAPI = connector_app(
    server,
    name="unitops",
    token_env="CHEMCLAW_UNITOPS_TOKEN",  # noqa: S106 - a variable's *name*, never a credential
    readiness=_readiness,
)
