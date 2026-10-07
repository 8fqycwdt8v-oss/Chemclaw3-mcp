"""The `props` server's FastAPI app.

    uvicorn chemclaw_mcp_props.app:app --host 127.0.0.1 --port 8850

`mcp_server_kit.connector_app` owns the shape. Bearer auth via `CHEMCLAW_PROPS_TOKEN` (the name
`connector.yaml` declares) is enforced even on loopback; unset, every request fails closed with 401.
"""

from fastapi import FastAPI
from mcp_server_kit import Dataset, connector_app

from chemclaw_mcp_props.engine import records
from chemclaw_mcp_props.tools import server


def _readiness() -> list[Dataset]:
    """Verify the solvent table, and name the version of it this pod serves.

    Nothing at import touches the table, so a corrupt corpus is a 503 naming the file rather than an
    import error and `CrashLoopBackOff`.
    """
    return [records.dataset()]


app: FastAPI = connector_app(
    server,
    name="props",
    token_env="CHEMCLAW_PROPS_TOKEN",  # noqa: S106 - an environment variable's *name*, never a credential
    readiness=_readiness,
)
