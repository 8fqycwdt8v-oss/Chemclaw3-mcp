"""The `thermalsafety` server's FastAPI app.

    uvicorn chemclaw_mcp_thermalsafety.app:app --host 127.0.0.1 --port 8851

Three lines of composition, because `mcp_server_kit.connector_app` owns the shape: the MCP session
manager's lifespan, the bearer check on `/mcp`, the caller logging, the body cap, the session
ceiling, `/healthz`, `/livez` and `/metrics`.

`CHEMCLAW_THERMALSAFETY_TOKEN` is the environment variable this server's `connector.yaml` declares,
and it is enforced even on the loopback dev URL — the manifest says bearer, so the server is bearer,
and an unset variable fails closed with 401 rather than serving the surface anonymously.

**The readiness check runs the arithmetic rather than loading anything**, because this server loads
nothing: see `engine/selftest.py`, which also records why the argument that it therefore needs no
probe was wrong. The short version is that the atomic-weight table and the screening bands are a
vendored corpus that happens to live in Python source, and a probe that recomputes published values
is what catches a transposed digit in one.
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
