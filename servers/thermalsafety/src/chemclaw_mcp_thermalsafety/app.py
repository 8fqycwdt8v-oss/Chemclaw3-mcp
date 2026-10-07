"""The `thermalsafety` server's FastAPI app.

    uvicorn chemclaw_mcp_thermalsafety.app:app --host 127.0.0.1 --port 8851

`mcp_server_kit.connector_app` owns the shape. Bearer auth on `CHEMCLAW_THERMALSAFETY_TOKEN`, as
`connector.yaml` declares, enforced even on loopback; an unset token fails closed with 401.
Readiness recomputes published values (`engine/selftest.py`), since the atomic weights and screening
bands are a corpus that lives in source.
"""

from fastapi import FastAPI
from mcp_server_kit import Dataset, connector_app

from chemclaw_mcp_thermalsafety.engine import selftest
from chemclaw_mcp_thermalsafety.tools import server


def _readiness() -> list[Dataset]:
    """Recompute the published cases, and name the constants this pod serves."""
    return selftest.verify()


app: FastAPI = connector_app(
    server,
    name="thermalsafety",
    token_env="CHEMCLAW_THERMALSAFETY_TOKEN",  # noqa: S106 - a variable's *name*, never a credential
    readiness=_readiness,
)
