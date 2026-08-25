"""The `pyexec` server's FastAPI app.

    uvicorn chemclaw_mcp_pyexec.app:app --host 127.0.0.1 --port 8899

Three lines of composition, because `mcp_server_kit.connector_app` owns the shape: the MCP session
manager's lifespan, the bearer check on `/mcp`, the caller logging, the body cap, `/healthz` and
`/metrics`.

`CHEMCLAW_PYEXEC_TOKEN` is the environment variable this server's `connector.yaml` declares, and it
matters more here than anywhere else in the fleet. Every other server refuses anonymous callers to
protect a *table*; this one refuses them to protect an interpreter. A missing token fails closed
with 401 rather than serving the surface anonymously, which is the arrangement Chemclaw3 got wrong
once and served every tool to anything that could reach the pod.
"""

from fastapi import FastAPI
from mcp_server_kit import connector_app

from chemclaw_mcp_pyexec.tools import server

app: FastAPI = connector_app(server, name="pyexec", token_env="CHEMCLAW_PYEXEC_TOKEN")
