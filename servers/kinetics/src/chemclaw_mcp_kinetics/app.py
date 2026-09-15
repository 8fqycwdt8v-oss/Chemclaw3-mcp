"""The `kinetics` server's FastAPI app.

    uvicorn chemclaw_mcp_kinetics.app:app --host 127.0.0.1 --port 8852

Three lines of composition, because `mcp_server_kit.connector_app` owns the shape: the MCP session
manager's lifespan, the bearer check on `/mcp`, the caller logging, the body cap, the session
ceiling, `/healthz`, `/livez` and `/metrics`.

`CHEMCLAW_KINETICS_TOKEN` is the environment variable this server's `connector.yaml` declares, and
it is enforced even on the loopback dev URL — the manifest says bearer, so the server is bearer, and
an unset variable fails closed with 401 rather than serving the surface anonymously.

**The readiness check runs the arithmetic against relations it does not contain**: the textbook
CSTR/PFR ratio, the exactness of the closed-form inverse at five orders, the integrator's
convergence *order*, and the doubling-per-10-degrees rule. The third of those has already earned its
place — it is what caught a feed-term discontinuity that left the answer right to three significant
figures and the convergence first-order. See `engine/selftest.py`.
"""

from fastapi import FastAPI
from mcp_server_kit import Dataset, connector_app

from chemclaw_mcp_kinetics.engine import selftest
from chemclaw_mcp_kinetics.tools import server


def _readiness() -> list[Dataset]:
    """Recompute the relations, and name the formulas this pod serves."""
    return selftest.verify()


app: FastAPI = connector_app(
    server,
    name="kinetics",
    token_env="CHEMCLAW_KINETICS_TOKEN",  # noqa: S106 - a variable's *name*, never a credential
    readiness=_readiness,
)
