"""What this server has to have loaded before it can answer — and the corpora that proves.

All five tables (structural rules, genotoxic alerts, ICH Q3C, ICH Q3D, reagents) load lazily, so
without this a pod with a corrupt table would pass its probe and refuse or mis-answer every screen.
The check runs the public screening entry points on ethanol, because a table can pass its checksum
and still hold a SMARTS that will not compile; the path is checked, not the answer. Cached: the
loaders are cached too, and this is a startup property.
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
        SafetyRulesError: A table is missing, unapproved, invalid, or holds an uncompilable pattern;
            `connector_app` answers 503 with the reason.
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
