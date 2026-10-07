"""The `pyexec` server's FastAPI app.

    uvicorn chemclaw_mcp_pyexec.app:app --host 127.0.0.1 --port 8899

`mcp_server_kit.connector_app` owns the shape. Bearer auth via `CHEMCLAW_PYEXEC_TOKEN` (the name
`connector.yaml` declares) protects an interpreter here; unset, every request fails closed with 401.
"""

from fastapi import FastAPI
from mcp_server_kit import Dataset, connector_app

from chemclaw_mcp_pyexec.engine.readiness import verify_sandbox
from chemclaw_mcp_pyexec.tools import server


def _readiness() -> list[Dataset]:
    """Prove this pod can fork, run and read back a program before it takes traffic.

    No corpus, so the dataset list is empty; `engine/readiness.verify_sandbox` runs one program
    through the same sandbox `run_python` uses.
    """
    return list(verify_sandbox())


app: FastAPI = connector_app(
    server,
    name="pyexec",
    token_env="CHEMCLAW_PYEXEC_TOKEN",  # noqa: S106 - an environment variable's *name*, never a credential
    readiness=_readiness,
)
