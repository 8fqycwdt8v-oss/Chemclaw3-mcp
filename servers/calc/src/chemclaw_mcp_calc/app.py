"""The `calc` server's FastAPI app.

    uvicorn chemclaw_mcp_calc.app:app --host 127.0.0.1 --port 8860

Four lines of composition, because `mcp_server_kit.connector_app` owns the shape: the MCP session
manager's lifespan, the bearer check on `/mcp`, the caller logging, the body cap, `/healthz` and
`/metrics`.

`CHEMCLAW_CALC_TOKEN` is the environment variable this server's `connector.yaml` declares, and the
same name is read on both sides — Chemclaw3 to send the token, this server to verify it. Chemclaw3's
own `calc` bundle declares `auth: {mode: none}` because it was only ever dialled over loopback from
the same pod; a server that lives in another repository and another image is dialled across a
network, so it declares bearer and enforces it even on the loopback dev URL. Unset, the variable
fails closed with 401 rather than serving the surface anonymously.

`on_start` is the one thing this server's app has that the other three do not, and it is not
decoration. Every tool derives a `calc_version`, and deriving one resolves the xTB backend — which
on an image carrying the `xtb` binary means a `subprocess` call with a 30 s timeout, once per
process. Without the hoist, whichever request arrived first in a fresh pod would pay it **on the
event loop**, stopping every other in-flight stream. See `tools.resolve_calculator_versions`.
"""

from __future__ import annotations

from fastapi import FastAPI
from mcp_server_kit import Dataset, connector_app

from chemclaw_mcp_calc.engine.pka import calc_version
from chemclaw_mcp_calc.tools import resolve_calculator_versions, server


def _readiness() -> list[Dataset]:
    """Prove this pod can derive a `calc_version`, which is what every tool here needs first.

    No dataset: this server vendors no corpus, so the list is empty and the payload says so rather
    than omitting the field. What it checks instead is the one resolution every result depends on —
    `calc_version` resolves the backend (tblite, or the `xtb` binary under `xtb_engine=auto`), and
    a pod that cannot resolve it cannot answer a single tool call.

    Deliberately stricter than `on_start`, which swallows the same failure: starting is not the
    same decision as taking traffic. A server that cannot name its calculator should not be sent a
    calculation — it would fail every one of them — and 503 on the probe is how a cluster is told
    that without the pod having to crash-loop.

    `calc_version` is a string built from cached backend lookups, so the probe pays the subprocess
    resolution at most once per process; `connector_app` runs it off the event loop regardless.
    """
    calc_version()
    return []


app: FastAPI = connector_app(
    server,
    name="calc",
    token_env="CHEMCLAW_CALC_TOKEN",
    on_start=resolve_calculator_versions,
    readiness=_readiness,
)
