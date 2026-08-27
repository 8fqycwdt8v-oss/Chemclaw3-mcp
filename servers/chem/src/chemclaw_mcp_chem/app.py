"""The `chem` server's FastAPI app.

    uvicorn chemclaw_mcp_chem.app:app --host 127.0.0.1 --port 8858

Three lines of composition, because `mcp_server_kit.connector_app` owns the shape: the MCP session
manager's lifespan, the bearer check on `/mcp`, the caller logging, the body cap, `/healthz` and
`/metrics`.

`CHEMCLAW_CHEM_TOKEN` is the environment variable this server's `connector.yaml` declares, and the
same name is read on both sides — Chemclaw3 to send the token, this server to verify it. Chemclaw3's
own `chem` bundle declares `auth: {mode: none}` because it was only ever dialled over loopback from
the same pod; a server that lives in another repository and another image is dialled across a
network, so it declares bearer and enforces it even on the loopback dev URL. Unset, the variable
fails closed with 401 rather than serving the surface anonymously.
"""

from fastapi import FastAPI
from mcp_server_kit import Dataset, connector_app

from chemclaw_mcp_chem.engine import reagents
from chemclaw_mcp_chem.tools import server


def _readiness() -> list[Dataset]:
    """Verify the reagent table, and name the version of it this pod serves.

    This server is the one the readiness gap was *demonstrated* on: nothing here touches the corpus
    at import, so a `chem` pod carrying a `records.csv` that failed its checksum returned 200 from
    `/healthz`, passed the kubelet probe, took traffic, and failed every tool call.
    """
    return [reagents.dataset()]


app: FastAPI = connector_app(
    server, name="chem", token_env="CHEMCLAW_CHEM_TOKEN", readiness=_readiness
)
