"""The `rxnpredict` server's FastAPI app.

    uvicorn chemclaw_mcp_rxnpredict.app:app --host 127.0.0.1 --port 8857

`connector_app` applies bearer auth as ASGI middleware, so `/mcp` is inside it (a mounted route
would bypass a route dependency). The token is `CHEMCLAW_RXNPREDICT_TOKEN`, as `connector.yaml`
declares; unset, every MCP request is refused with 401.
"""

from fastapi import FastAPI
from mcp_server_kit import Dataset, connector_app

from chemclaw_mcp_rxnpredict.engine.config import DATA_DIR, get_settings
from chemclaw_mcp_rxnpredict.engine.meta.trust_priors import priors_dataset
from chemclaw_mcp_rxnpredict.engine.readiness import verify_predictors
from chemclaw_mcp_rxnpredict.tools import server


def _readiness() -> list[Dataset]:
    """Load and checksum the trust priors, and verify the predictor ensemble, off the request path.

    `Settings.class_priors()` reads `trust_priors.json` lazily, so a corrupt corpus is this probe's
    503 rather than an import error. `verify_predictors` catches a pod whose installed predictors
    failed to register; see `engine/readiness.py` for why an absent extra is ready and a broken one
    is not.
    """
    get_settings().class_priors()
    verify_predictors()
    return [priors_dataset(DATA_DIR)]


app: FastAPI = connector_app(
    server,
    name="rxnpredict",
    token_env="CHEMCLAW_RXNPREDICT_TOKEN",  # noqa: S106 - an environment variable's *name*, never a credential
    readiness=_readiness,
)
