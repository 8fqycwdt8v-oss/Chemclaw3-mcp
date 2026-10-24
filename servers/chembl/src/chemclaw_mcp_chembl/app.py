"""The `chembl` server's FastAPI app.

    uvicorn chemclaw_mcp_chembl.app:app --host 127.0.0.1 --port 8862

`mcp_server_kit.connector_app` owns the shape. Bearer auth on `CHEMCLAW_CHEMBL_TOKEN`, as
`connector.yaml` declares, enforced even on loopback; an unset token fails closed with 401.
"""

from fastapi import FastAPI
from mcp_server_kit import connector_app

from chemclaw_mcp_chembl.engine.readiness import verified_corpora
from chemclaw_mcp_chembl.tools import server

app: FastAPI = connector_app(
    server,
    name="chembl",
    token_env="CHEMCLAW_CHEMBL_TOKEN",  # noqa: S106 - an environment variable's *name*, never a credential
    # One checksummed SQLite file: an unready pod would answer "not in the subset" for every
    # compound, which reads as a finding. `engine/readiness.py` runs a lookup and a summary.
    readiness=verified_corpora,
)
