"""The `rxnlabel` server's FastAPI app.

    uvicorn chemclaw_mcp_rxnlabel.app:app --host 127.0.0.1 --port 8865

`CHEMCLAW_RXNLABEL_TOKEN` is the environment variable this server's `connector.yaml` declares and
the one Chemclaw3's `rxnlabel_server_token_env` points at by default. Enforced rather than made
conditional on the address: the manifest declares bearer auth, so a missing token fails closed with
401 rather than serving the surface anonymously.

`on_start` logs what actually loaded. This server's answers differ depending on which optional
extras are installed, and an operator looking at a corpus that came back unnamed needs to be able
to see, in the first lines of the log, whether the classifier was there at all.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from mcp_server_kit import connector_app

from chemclaw_mcp_rxnlabel.engine import version
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


app: FastAPI = connector_app(
    server, name="rxnlabel", token_env="CHEMCLAW_RXNLABEL_TOKEN", on_start=_report_components
)
