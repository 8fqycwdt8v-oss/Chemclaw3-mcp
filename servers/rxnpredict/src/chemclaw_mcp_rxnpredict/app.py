"""The `rxnpredict` server's FastAPI app.

    uvicorn chemclaw_mcp_rxnpredict.app:app --host 127.0.0.1 --port 8857

Three lines, because `mcp_server_kit.connector_app` owns the shape. That is the substantive
difference from upstream, which mounted `fastapi-mcp` over its REST routes and applied its bearer
check as a route `Depends(...)` — a mount bypasses the enclosing app's dependencies, so the
credential guarded the REST surface and the MCP surface was the one that mattered. Here the check
is ASGI middleware and `/mcp` is inside it.

`CHEMCLAW_RXNPREDICT_TOKEN` is the variable this server's `connector.yaml` declares, read on both
sides: Chemclaw3 to send it, this server to verify it. Unset, every MCP request is refused with 401
rather than served anonymously.
"""

from fastapi import FastAPI
from mcp_server_kit import connector_app

from chemclaw_mcp_rxnpredict.tools import server

app: FastAPI = connector_app(server, name="rxnpredict", token_env="CHEMCLAW_RXNPREDICT_TOKEN")
