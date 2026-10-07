"""The `chem` server's FastAPI app.

    uvicorn chemclaw_mcp_chem.app:app --host 127.0.0.1 --port 8858

`mcp_server_kit.connector_app` owns the shape. Bearer auth via `CHEMCLAW_CHEM_TOKEN` (the name
`connector.yaml` declares) is enforced even on loopback; unset, every request fails closed with 401.
"""

from fastapi import FastAPI
from mcp_server_kit import Dataset, connector_app

from chemclaw_mcp_chem.engine import reagents
from chemclaw_mcp_chem.tools import server


def _readiness() -> list[Dataset]:
    """Verify the reagent table, and name the version of it this pod serves.

    Nothing touches the corpus at import, so this is what makes a corrupt `records.csv` a 503.
    """
    return [reagents.dataset()]


app: FastAPI = connector_app(
    server,
    name="chem",
    token_env="CHEMCLAW_CHEM_TOKEN",  # noqa: S106 - an environment variable's *name*, never a credential
    readiness=_readiness,
)
