"""Deterministic content-addressed hashing — the identity scheme Chemclaw3's keys are built on.

**A copy of Chemclaw3's `chemclaw/core/ids.py`, and the copy that matters most in this
repository.** The other two servers copy a *canonicalizer*; this one copies the function whose
output becomes half of a cache key and half of a calibration-ledger primary key. If the algorithm,
the digest width, the JSON separators or the sort order drifted from Chemclaw3's, every
`input_hash` this server emits would address nothing on the other side — and it would do so
silently, because a hash that no row matches is indistinguishable from a calculation nobody has
run before.

So this file is deliberately minimal, and `tests/test_key_contract.py` pins its output as literal
strings produced by running Chemclaw3's own function.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = ["stable_hash"]

# Default digest width for a content-addressed key. 16 hex chars = 64 bits: enough that a collision
# between two distinct calculations is not a practical concern. **Not a knob** — it is part of the
# wire contract with Chemclaw3's `calculation_results` and `predictions` tables.
_DEFAULT_CHARS = 16


def stable_hash(payload: Any, *, chars: int = _DEFAULT_CHARS) -> str:
    """Return a stable short SHA-256 of the canonical JSON form of `payload`.

    Sorted keys and tight separators make the hash independent of dict ordering and whitespace, so
    semantically identical inputs collapse to the same key. `default=str` lets values that are not
    JSON-native serialize deterministically.

    Args:
        payload: Any JSON-serializable value (mapping, list, scalar).
        chars: Number of leading hex characters to keep (4 bits each).
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:chars]
