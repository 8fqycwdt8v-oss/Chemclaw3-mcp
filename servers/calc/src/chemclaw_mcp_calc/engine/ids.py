"""Deterministic content-addressed hashing — the identity scheme Chemclaw3's keys are built on.

A copy of Chemclaw3's `chemclaw/core/ids.py`. Its output is half of every cache and ledger key,
so any drift in algorithm, width, separators or ordering would silently address nothing;
`tests/test_key_contract.py` pins it against Chemclaw3's literal outputs.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = ["stable_hash"]

# Digest width: 16 hex chars (64 bits). Part of the wire contract with Chemclaw3, not a knob.
_DEFAULT_CHARS = 16


def stable_hash(payload: Any, *, chars: int = _DEFAULT_CHARS) -> str:
    """Return a stable short SHA-256 of the canonical JSON form of `payload`.

    Sorted keys and tight separators make the hash independent of ordering and whitespace;
    `default=str` serialises non-JSON values deterministically.

    Args:
        payload: Any JSON-serializable value (mapping, list, scalar).
        chars: Number of leading hex characters to keep (4 bits each).
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:chars]
