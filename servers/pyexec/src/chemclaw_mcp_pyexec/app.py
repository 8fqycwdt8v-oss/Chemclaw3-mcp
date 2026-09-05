"""The `pyexec` server's FastAPI app.

    uvicorn chemclaw_mcp_pyexec.app:app --host 127.0.0.1 --port 8899

Composition, because `mcp_server_kit.connector_app` owns the shape: the MCP session manager's
lifespan, the bearer check on `/mcp`, the caller logging, the body cap, `/healthz` and `/metrics`.

`CHEMCLAW_PYEXEC_TOKEN` is the environment variable this server's `connector.yaml` declares, and it
matters more here than anywhere else in the fleet. Every other server refuses anonymous callers to
protect a *table*; this one refuses them to protect an interpreter. A missing token fails closed
with 401 rather than serving the surface anonymously, which is the arrangement Chemclaw3 got wrong
once and served every tool to anything that could reach the pod.
"""

from fastapi import FastAPI
from mcp_server_kit import Dataset, connector_app

from chemclaw_mcp_pyexec.engine.readiness import verify_sandbox
from chemclaw_mcp_pyexec.tools import server


def _readiness() -> list[Dataset]:
    """Prove this pod can actually fork, run and read back a program before it takes traffic.

    No dataset: this server vendors no corpus, so the list is empty and `/healthz` publishes
    `datasets: []` rather than omitting the field. What it checks instead is the only thing this
    server does — `engine/readiness.verify_sandbox` runs one trivial program through the same
    sandbox `run_python` uses and checks what came back. Nothing here is touched at import, so
    without it a pod with a broken interpreter or an unwritable scratch directory answered a
    constant 200 and failed every call.
    """
    return list(verify_sandbox())


app: FastAPI = connector_app(
    server, name="pyexec", token_env="CHEMCLAW_PYEXEC_TOKEN", readiness=_readiness
)
