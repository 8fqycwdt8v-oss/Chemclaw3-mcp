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
from chemclaw_mcp_rxnpredict.engine.readiness import verify_predictors
from chemclaw_mcp_rxnpredict.tools import server


def _readiness() -> list[Dataset]:
    """Force the per-class trust-priors load every tool call already triggers, off the request path.

    `Settings.class_priors()` is where `trust_priors.json` is read and checksummed — inside the
    first aggregation that wants it, not at import. Every other server with a vendored corpus
    (`props`, `chem`, `safety`, `calc`) passes a `readiness` callable for exactly this reason; this
    one shipped without it, so a `trust_priors.json` that failed its checksum would have passed
    `/healthz`, taken traffic, and failed every real prediction — the same gap `chem`'s `_readiness`
    docstring documents having been caught on.

    **The sentence this docstring used to open with was false, and fixing it is what closed a
    backlog row** — see
    `D-2026-09-18-a-corpus-that-cannot-be-read-is-a-probe-s-answer-not-an-import-error`.
    It said the load happened "inside the *first tool call*, not at import", and cited
    `config.py`'s `get_settings` docstring as the authority. Measured: `tools.py` calls
    `register_requested()` at module scope, `register_requested` calls `get_settings()`, and
    `get_settings()` loaded the corpus — so one byte appended to `trust_priors.json` raised
    `DatasetError` out of `import chemclaw_mcp_rxnpredict.tools` and this probe never ran at all.
    `get_settings()` is pure environment now, and the load is `class_priors()`, called here.

    `priors_dataset` is `lru_cache`d, so naming the version here after `get_settings()` has already
    loaded it costs nothing further.

    **And the corpus was never the thing most likely to be missing here.** This server's answer is
    an *ensemble*, assembled at import from optional predictor modules, and a module that raised
    was recorded and logged and nothing more — so a pod that had lost every forward predictor
    passed this probe and raised on every call. `verify_predictors` is that half; see
    `engine/readiness.py` for why an absent extra is ready and a broken one is not.
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
