"""The `suitability` server's FastAPI app.

    uvicorn chemclaw_mcp_suitability.app:app --host 127.0.0.1 --port 8892

Three lines of composition, because `mcp_server_kit.connector_app` owns the shape: the MCP session
manager's lifespan, the bearer check on `/mcp`, the caller logging, the body cap, the session
ceiling, `/healthz`, `/livez` and `/metrics`.

`CHEMCLAW_SUITABILITY_TOKEN` is the environment variable this server's `connector.yaml` declares,
and it is enforced even on the loopback dev URL — the manifest says bearer, so the server is
bearer, and an unset variable fails closed with 401 rather than serving the surface anonymously.

**The readiness check runs the arithmetic rather than loading anything.** This server has no
corpus, and `D-2026-09-15-a-server-with-nothing-to-load-still-has-something-to-verify` already
settled that this does not excuse a probe. What it verifies is unusually strong here: USP's own
constants over-determine each other, so the two plate-count forms and the two resolution forms must
agree on a Gaussian peak, and a transposed digit in 5.54 or 1.18 breaks that agreement immediately.
See `engine/selftest.py`.
"""

from fastapi import FastAPI
from mcp_server_kit import Dataset, connector_app

from chemclaw_mcp_suitability.engine import selftest
from chemclaw_mcp_suitability.tools import server


def _readiness() -> list[Dataset]:
    """Recompute the constants against each other, and name what this pod serves."""
    return selftest.verify()


app: FastAPI = connector_app(
    server,
    name="suitability",
    token_env="CHEMCLAW_SUITABILITY_TOKEN",  # noqa: S106 - a variable's *name*, never a credential
    readiness=_readiness,
)
