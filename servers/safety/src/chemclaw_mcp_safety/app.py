"""The `safety` server's FastAPI app.

    uvicorn chemclaw_mcp_safety.app:app --host 127.0.0.1 --port 8859

`mcp_server_kit.connector_app` owns the shape. Bearer auth on `CHEMCLAW_SAFETY_TOKEN`, as
`connector.yaml` declares, enforced even on loopback; an unset token fails closed with 401.
"""

from fastapi import FastAPI
from mcp_server_kit import connector_app

from chemclaw_mcp_safety.engine.readiness import verified_corpora
from chemclaw_mcp_safety.tools import server

app: FastAPI = connector_app(
    server,
    name="safety",
    token_env="CHEMCLAW_SAFETY_TOKEN",  # noqa: S106 - an environment variable's *name*, never a credential
    # Five lazily loaded tables; an unready pod would answer "nothing matched", which reads as safe.
    # `engine/readiness.py` exercises the screens, not just the hashes.
    readiness=verified_corpora,
)
