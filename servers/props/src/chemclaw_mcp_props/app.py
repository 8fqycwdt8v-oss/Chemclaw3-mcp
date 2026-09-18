"""The `props` server's FastAPI app.

    uvicorn chemclaw_mcp_props.app:app --host 127.0.0.1 --port 8850

Three lines of composition, because `mcp_server_kit.connector_app` owns the shape: the MCP session
manager's lifespan, the bearer check on `/mcp`, the caller logging, the body cap, `/healthz` and
`/metrics`.

`CHEMCLAW_PROPS_TOKEN` is the environment variable this server's `connector.yaml` declares. Left
unset in a loopback dev run, `token_env=None` here would be the alternative — but it is *not* taken:
the manifest declares bearer auth, so the server enforces it, and a missing token fails closed with
401 rather than serving the surface anonymously. Chemclaw3 shipped the other arrangement once and
served every tool to anything that could reach the pod.
"""

from fastapi import FastAPI
from mcp_server_kit import Dataset, connector_app

from chemclaw_mcp_props.engine import records
from chemclaw_mcp_props.tools import server


def _readiness() -> list[Dataset]:
    """Verify the solvent table, and name the version of it this pod serves.

    `/healthz` was a constant 200 before this, and on this server it happened to be *right* — but
    only by accident: `tools.py` computed `MAX_COMPARED_SOLVENTS` as `len(records.all_solvents())`
    at module scope, so the corpus was loaded and checksummed at import. Measured beside `chem`,
    which does not and returned 200 healthy with a corpus that failed its checksum. Relying on an
    incidental module-level call is not a readiness check, and this is now the only one: that line
    is gone (`D-2026-09-18-a-corpus-that-cannot-be-read-is-a-probe-s-answer-not-an-import-error`),
    because an import that verifies a corpus is an import that *fails* on a corrupt one — which
    takes the pod down with `CrashLoopBackOff` in place of a 503 naming the file and both hashes.
    Nothing this server does at import touches the table any more, and
    `tests/test_fleet.py::test_a_corrupt_corpus_is_the_probe_s_answer_rather_than_an_import_error`
    is what keeps it that way for every server in the fleet that loads one.
    """
    return [records.dataset()]


app: FastAPI = connector_app(
    server,
    name="props",
    token_env="CHEMCLAW_PROPS_TOKEN",  # noqa: S106 - an environment variable's *name*, never a credential
    readiness=_readiness,
)
