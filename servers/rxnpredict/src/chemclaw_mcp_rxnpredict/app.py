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

The one addition over `props`' three lines is `on_start`, which says out loud what this process is
holding: which predictors loaded, and whether the baked weights match the digests the build
recorded for them. Logged rather than enforced, deliberately — the Containerfile already fails the
*build* on a bad copy, and a server that refused to start over a weights manifest would refuse in
every deployment that runs the deterministic doubles or none at all.
"""

import logging

from fastapi import FastAPI
from mcp_server_kit import connector_app

from chemclaw_mcp_rxnpredict.engine.config import get_settings
from chemclaw_mcp_rxnpredict.engine.predictors import list_conditions, list_forward, unavailable
from chemclaw_mcp_rxnpredict.engine.weights import verify_weights
from chemclaw_mcp_rxnpredict.tools import server

logger = logging.getLogger(__name__)


async def _report() -> None:
    """Log what this process actually loaded. Never raises — see the module docstring."""
    try:
        forward = sorted(p.name for p in list_forward())
        conditions = sorted(p.name for p in list_conditions())
        logger.info(
            "rxnpredict ready: forward=%s conditions=%s unavailable=%d",
            forward or ["none"],
            conditions or ["none"],
            len(unavailable()),
        )
        report = verify_weights(get_settings().model_dir)
        (logger.info if report.verified else logger.error)("rxnpredict: %s", report.summary())
    except Exception:  # pragma: no cover - a report that fails must not take the server with it
        logger.exception("rxnpredict: could not describe what this process loaded")


app: FastAPI = connector_app(
    server,
    name="rxnpredict",
    token_env="CHEMCLAW_RXNPREDICT_TOKEN",
    on_start=_report,
)
