"""What this server has to have loaded before it can answer — and the check that proves it.

The subset is one SQLite file read lazily, so without this a pod with a corrupt or foreign file
would pass its probe and answer "not in the subset" for every compound. The check verifies the
checksum and schema version, then runs both public entry points on aspirin (CHEMBL25, in every
tier), because a file can pass its checksum and still lack an index or a table: the path is
checked, not the answer. Cached: it is a startup property.
"""

from __future__ import annotations

from functools import lru_cache

from mcp_server_kit import Dataset

from chemclaw_mcp_chembl.engine import store
from chemclaw_mcp_chembl.engine.bioactivity import bioactivity_summary
from chemclaw_mcp_chembl.engine.lookup import lookup

__all__ = ["PROBE_ID", "verified_corpora"]

PROBE_ID = "CHEMBL25"


@lru_cache(maxsize=1)
def verified_corpora() -> tuple[Dataset, ...]:
    """Verify the subset and exercise every query path; return what was verified.

    Raises:
        DatasetError: The file is missing, unapproved or its manifest is invalid.
        StoreError: The file's schema version is not the one this server reads.
        RuntimeError: A query path returned nothing for the probe compound.
    """
    found = store.dataset()
    if not lookup(PROBE_ID).found or not lookup("aspirin", "name").found:
        raise RuntimeError(f"the ChEMBL subset does not hold {PROBE_ID}; it is not a usable subset")
    if not bioactivity_summary(PROBE_ID, limit=1).found:
        raise RuntimeError("the ChEMBL subset's activity tables could not be read")
    return (found,)
