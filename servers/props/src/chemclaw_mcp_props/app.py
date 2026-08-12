"""The `props` server's FastAPI app.

    uvicorn chemclaw_mcp_props.app:app --host 127.0.0.1 --port 8850

Three lines of composition, because `mcp_server_kit.connector_app` owns the shape: the MCP session
manager's lifespan, the bearer check on `/mcp`, the caller logging, the body cap, `/healthz` and
`/metrics`.

`CHEMCLAW_PROPS_TOKEN` is the environment variable this server's `connector.yaml` declares. Left
unset in a loopback dev run, `token_env=None` here would be the alternative — but it is *not* taken:
the manifest declares bearer auth, so the server enforces it, and a missing token fails closed with
401 rather than serving the surface anonymously. Chemclaw3 shipped the other arrangement once and
served every tool to anything that could reach the pod.
"""

from fastapi import FastAPI
from mcp_server_kit import connector_app

from chemclaw_mcp_props.tools import server

app: FastAPI = connector_app(server, name="props", token_env="CHEMCLAW_PROPS_TOKEN")
