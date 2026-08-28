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
from mcp_server_kit import Dataset, connector_app

from chemclaw_mcp_rxnpredict.engine.config import DATA_DIR, get_settings
from chemclaw_mcp_rxnpredict.engine.meta.trust_priors import priors_dataset
from chemclaw_mcp_rxnpredict.tools import server


def _readiness() -> list[Dataset]:
    """Force the per-class trust-priors load every tool call already triggers, off the request path.

    `get_settings()` is where `trust_priors.json` is actually read and checksummed — inside the
    *first tool call*, not at import (`config.py`'s `get_settings` docstring says so), because
    `Settings()` is plain pydantic construction and the vendored table is loaded lazily beside it.
    Every other server with a vendored corpus (`props`, `chem`, `safety`, `calc`) passes a
    `readiness` callable for exactly this reason; this one shipped without it, so a
    `trust_priors.json` that failed its checksum would have passed `/healthz`, taken traffic, and
    failed every real prediction — the same gap `chem`'s `_readiness` docstring documents having
    been caught on.

    `priors_dataset` is `lru_cache`d, so naming the version here after `get_settings()` has already
    loaded it costs nothing further.
    """
    get_settings()
    return [priors_dataset(DATA_DIR)]


app: FastAPI = connector_app(
    server, name="rxnpredict", token_env="CHEMCLAW_RXNPREDICT_TOKEN", readiness=_readiness
)
