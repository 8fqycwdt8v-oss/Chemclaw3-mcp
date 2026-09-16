"""The `unitops` server's FastAPI app.

    uvicorn chemclaw_mcp_unitops.app:app --host 127.0.0.1 --port 8853

Three lines of composition, because `mcp_server_kit.connector_app` owns the shape: the MCP session
manager's lifespan, the bearer check on `/mcp`, the caller logging, the body cap, the session
ceiling, `/healthz`, `/livez` and `/metrics`.

`CHEMCLAW_UNITOPS_TOKEN` is the environment variable this server's `connector.yaml` declares, and
it is enforced even on the loopback dev URL — the manifest says bearer, so the server is bearer, and
an unset variable fails closed with 401 rather than serving the surface anonymously.

**The readiness check runs every correlation against a relation it does not contain**: a column at
total reflux reducing to Fenske, Underwood's bisected root against the closed-form binary minimum
reflux, a crystallisation yield against the saturated-charge closed form, the 63.2% first-order
thermal approach, the geometric-similarity scale-up rule, Zwietering's dimensional homogeneity, a
parabolic filtration reporting its own derivative, and the two drying periods meeting at the
critical moisture. See `engine/selftest.py`.
"""

from fastapi import FastAPI
from mcp_server_kit import Dataset, connector_app

from chemclaw_mcp_unitops.engine import selftest
from chemclaw_mcp_unitops.tools import server


def _readiness() -> list[Dataset]:
    """Recompute the relations, and name the correlations this pod serves."""
    return selftest.verify()


app: FastAPI = connector_app(
    server,
    name="unitops",
    token_env="CHEMCLAW_UNITOPS_TOKEN",  # noqa: S106 - a variable's *name*, never a credential
    readiness=_readiness,
)
