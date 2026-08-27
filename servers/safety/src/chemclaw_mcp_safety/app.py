"""The `safety` server's FastAPI app.

    uvicorn chemclaw_mcp_safety.app:app --host 127.0.0.1 --port 8859

Three lines of composition, because `mcp_server_kit.connector_app` owns the shape: the MCP session
manager's lifespan, the bearer check on `/mcp`, the caller logging, the body cap, `/healthz` and
`/metrics`.

`CHEMCLAW_SAFETY_TOKEN` is the environment variable this server's `connector.yaml` declares, and the
same name is read on both sides — Chemclaw3 to send the token, this server to verify it. Chemclaw3's
own `safety` bundle declares `auth: {mode: none}` because it was only ever dialled over loopback
from the same pod; a server that lives in another repository and another image is dialled across a
network, so it declares bearer and enforces it even on the loopback dev URL. Unset, the variable
fails closed with 401 rather than serving the surface anonymously.
"""

from fastapi import FastAPI
from mcp_server_kit import connector_app

from chemclaw_mcp_safety.engine.readiness import verified_corpora
from chemclaw_mcp_safety.tools import server

app: FastAPI = connector_app(
    server,
    name="safety",
    token_env="CHEMCLAW_SAFETY_TOKEN",
    # Five tables, all loaded lazily, and an unready pod here answers "nothing matched" — which
    # reads as *safe*. See `engine/readiness.py` for why the check exercises the screens rather
    # than only hashing the files.
    readiness=verified_corpora,
)
