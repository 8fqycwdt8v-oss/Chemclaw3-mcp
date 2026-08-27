"""What this server has to have loaded before it can answer — and the corpora that proves.

`/healthz` used to be a constant 200, and on this server that was the most dangerous version of
that defect in the fleet. All four tables load *lazily*, on the first tool call that needs them, so
a pod whose `rules.yaml` failed its checksum passed the kubelet probe, took traffic, and refused
every screen — while `load_dataset`'s own docstring says a bad corpus "fails at startup with the
two hashes in the message", which is true only of a server that touches its corpus at import.

Written against the **public** screening entry points rather than the loaders behind them, because
readiness here means "this server can answer", not "these five files hash correctly": the alert and
hazard tables are compiled SMARTS, and a rule whose pattern does not parse is a table that passes
its checksum and cannot screen anything. Ethanol is the probe molecule for the obvious reason —
it is in the corpus, it is not hazardous, and what is being checked is the *path*, not the answer.

Cached, because this is a startup property: the loaders underneath are all `lru_cache`d, so a
30-second probe interval must not re-hash five files forever.
"""

from __future__ import annotations

from functools import lru_cache

from mcp_server_kit import Dataset, load_dataset

from chemclaw_mcp_safety.engine import reagents
from chemclaw_mcp_safety.engine.genotox import ALERTS_DIR, ALERTS_FILE, screen_genotoxic_alerts
from chemclaw_mcp_safety.engine.ich import Q3C_DIR, Q3C_FILE, Q3D_DIR, Q3D_FILE, index
from chemclaw_mcp_safety.engine.screen import RULES_DIR, RULES_FILE, screen_structure

__all__ = ["verified_corpora"]

_PROBE = "CCO"


@lru_cache(maxsize=1)
def verified_corpora() -> tuple[Dataset, ...]:
    """Load and exercise every table this server answers from; return what was verified.

    Raises:
        SafetyRulesError: a table is missing, is not the file its manifest approved, does not
            validate, or carries a pattern that will not compile. `connector_app` turns that into
            a 503 naming the reason, which is the whole point: an unready pod must not take
            traffic and then report "nothing matched" for every molecule.
    """
    screen_structure(_PROBE)
    screen_genotoxic_alerts([_PROBE])
    index()
    return (
        load_dataset(RULES_DIR, records_file=RULES_FILE),
        load_dataset(ALERTS_DIR, records_file=ALERTS_FILE),
        load_dataset(Q3C_DIR, records_file=Q3C_FILE),
        load_dataset(Q3D_DIR, records_file=Q3D_FILE),
        reagents.dataset(),
    )
