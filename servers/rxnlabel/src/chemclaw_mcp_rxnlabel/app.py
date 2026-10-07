"""The `rxnlabel` server's FastAPI app.

    uvicorn chemclaw_mcp_rxnlabel.app:app --host 127.0.0.1 --port 8865

Bearer auth on `CHEMCLAW_RXNLABEL_TOKEN`, as `connector.yaml` declares; an unset token fails closed
with 401. `on_start` logs which optional components loaded, since answers depend on them.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from mcp_server_kit import Dataset, connector_app

from chemclaw_mcp_rxnlabel.engine import version
from chemclaw_mcp_rxnlabel.engine.readiness import verify_labeller
from chemclaw_mcp_rxnlabel.tools import server

logger = logging.getLogger(__name__)


async def _report_components() -> None:
    """Log the labeller version and its components once at startup."""
    components = version.components()
    logger.info("rxnlabel version %s (%s)", version.labeller_version(), components)
    if components["atom_mapper"] == "absent":
        logger.warning(
            "no atom mapper installed: reactants and reagents are separated by the slot they were "
            "written in rather than by an atom map, which is coarser. Rows labelled here re-label "
            "automatically once the `models` extra is installed."
        )
    if components["reaction_namer"] == "absent":
        logger.warning(
            "no reaction classifier installed: every reaction is labelled without a name. Rows "
            "labelled here re-label automatically once the `models` extra is installed."
        )


def _readiness() -> list[Dataset]:
    """Prove this pod can label, and that no component it installed is silently broken.

    No vendored corpus, so the dataset list is empty. See `engine/readiness.py` for why an installed
    component that will not construct fails readiness while an absent one does not.
    """
    return list(verify_labeller())


app: FastAPI = connector_app(
    server,
    name="rxnlabel",
    token_env="CHEMCLAW_RXNLABEL_TOKEN",  # noqa: S106 - an environment variable's *name*, never a credential
    on_start=_report_components,
    readiness=_readiness,
)
