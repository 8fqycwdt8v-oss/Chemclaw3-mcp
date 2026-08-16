"""The `calc` server's FastAPI app.

    uvicorn chemclaw_mcp_calc.app:app --host 127.0.0.1 --port 8860

Four lines of composition, because `mcp_server_kit.connector_app` owns the shape: the MCP session
manager's lifespan, the bearer check on `/mcp`, the caller logging, the body cap, `/healthz` and
`/metrics`.

`CHEMCLAW_CALC_TOKEN` is the environment variable this server's `connector.yaml` declares, and the
same name is read on both sides — Chemclaw3 to send the token, this server to verify it. Chemclaw3's
own `calc` bundle declares `auth: {mode: none}` because it was only ever dialled over loopback from
the same pod; a server that lives in another repository and another image is dialled across a
network, so it declares bearer and enforces it even on the loopback dev URL. Unset, the variable
fails closed with 401 rather than serving the surface anonymously.

`on_start` is the one thing this server's app has that the other three do not, and it is not
decoration. Every tool derives a `calc_version`, and deriving one resolves the xTB backend — which
on an image carrying the `xtb` binary means a `subprocess` call with a 30 s timeout, once per
process. Without the hoist, whichever request arrived first in a fresh pod would pay it **on the
event loop**, stopping every other in-flight stream. See `tools.resolve_calculator_versions`.
"""

from __future__ import annotations

from fastapi import FastAPI
from mcp_server_kit import connector_app

from chemclaw_mcp_calc.tools import resolve_calculator_versions, server

app: FastAPI = connector_app(
    server,
    name="calc",
    token_env="CHEMCLAW_CALC_TOKEN",
    on_start=resolve_calculator_versions,
)
