"""Deterministic content-addressed hashing — the identity scheme Chemclaw3's keys are built on.

The fleet's one definition lives in `mcp_server_kit.keys`; this module re-exports it under the
name every `calc` engine module imports. Its output is half of every cache and ledger key, and
`tests/test_key_contract.py` pins it against Chemclaw3's literal outputs.
"""

from __future__ import annotations

from mcp_server_kit.keys import stable_hash

__all__ = ["stable_hash"]
