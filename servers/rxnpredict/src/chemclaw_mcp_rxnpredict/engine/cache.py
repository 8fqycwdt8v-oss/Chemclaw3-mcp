"""The prediction cache: bounded, in-process, and deliberately not on disk.

Upstream backed this with `diskcache` under `~/.cache/chemclaw2_forward`. That cannot work here.
The image is rootless with a read-only root filesystem, so the cache would need a writable volume —
and a volume exists to hold something worth keeping across a restart, which a memo of a
deterministic function is not.

So it is an LRU in the process, and three things follow that are worth stating rather than
discovering:

- **A restart loses the cache.** Accepted: the win this exists for is a repeated call *inside a
  conversation* — the agent asking for the same reaction twice while reasoning — and that is
  entirely within one process lifetime.
- **There is no TTL.** A model's prediction for a reaction does not go stale; only the model does,
  and that changes with a new image.
- **There is nothing to clear**, which is why this server exposes no `clear_prediction_cache` tool
  and therefore has no state-changing surface at all. `clear()` stays for tests.

The key is the same one upstream used — predictor, *canonical* reactants (and product), top_k — so
two spellings of one reaction share a slot. Canonicalisation is best-effort: an input RDKit cannot
parse is keyed by its raw text rather than raising, because a cache must never be the thing that
fails a prediction.
"""

from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict
from typing import Any

from chemclaw_mcp_rxnpredict.engine.preprocessing import canonical_multi_smiles, canonical_smiles

logger = logging.getLogger(__name__)

Payload = list[dict[str, Any]]


def _hash_key(parts: list[str]) -> str:
    """A stable digest of the key parts, so one string keys the entry however long the SMILES."""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _safe_canon_reactants(smiles: str) -> str:
    """Canonical multi-component SMILES, falling back to the raw text if RDKit refuses it."""
    try:
        return canonical_multi_smiles(smiles)
    except Exception:
        return smiles


def _safe_canon_product(smiles: str) -> str:
    """Canonical single-component SMILES, with the same fallback."""
    try:
        return canonical_smiles(smiles)
    except Exception:
        return smiles


class PredictionCache:
    """A bounded LRU over predictor results. Disabled, it is a no-op with the same interface."""

    def __init__(self, *, enabled: bool, max_entries: int) -> None:
        """Hold at most `max_entries` results, evicting least-recently-used first."""
        self.enabled = enabled and max_entries > 0
        self._max_entries = max_entries
        self._entries: OrderedDict[str, Payload] = OrderedDict()

    def _get(self, key: str) -> Payload | None:
        """Fetch and mark as most-recently-used."""
        if not self.enabled:
            return None
        found = self._entries.get(key)
        if found is None:
            return None
        self._entries.move_to_end(key)
        return found

    def _set(self, key: str, payload: Payload) -> None:
        """Store, evicting the least-recently-used entry once the bound is passed."""
        if not self.enabled:
            return
        self._entries[key] = payload
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def _key_forward(self, model_name: str, reactants: str, top_k: int) -> str:
        """Key for a forward prediction."""
        return _hash_key(["fwd", model_name, _safe_canon_reactants(reactants), str(top_k)])

    def _key_conditions(self, model_name: str, reactants: str, product: str, top_k: int) -> str:
        """Key for a conditions prediction."""
        return _hash_key(
            [
                "cond",
                model_name,
                _safe_canon_reactants(reactants),
                _safe_canon_product(product),
                str(top_k),
            ]
        )

    def get_forward(self, model_name: str, reactants: str, top_k: int) -> Payload | None:
        """A cached forward result, or `None`."""
        return self._get(self._key_forward(model_name, reactants, top_k))

    def set_forward(self, model_name: str, reactants: str, top_k: int, payload: Payload) -> None:
        """Store a forward result."""
        self._set(self._key_forward(model_name, reactants, top_k), payload)

    def get_conditions(
        self, model_name: str, reactants: str, product: str, top_k: int
    ) -> Payload | None:
        """A cached conditions result, or `None`."""
        return self._get(self._key_conditions(model_name, reactants, product, top_k))

    def set_conditions(
        self, model_name: str, reactants: str, product: str, top_k: int, payload: Payload
    ) -> None:
        """Store a conditions result."""
        self._set(self._key_conditions(model_name, reactants, product, top_k), payload)

    def clear(self) -> int:
        """Drop everything, returning how many entries went. For tests."""
        count = len(self._entries)
        self._entries.clear()
        return count


_cache: PredictionCache | None = None


def get_cache() -> PredictionCache:
    """The process-wide cache, built from settings on first use."""
    global _cache
    if _cache is None:
        from chemclaw_mcp_rxnpredict.engine.config import get_settings

        settings = get_settings()
        _cache = PredictionCache(
            enabled=settings.cache_enabled, max_entries=settings.cache_max_entries
        )
    return _cache


def reset_cache_for_tests() -> None:
    """Force a fresh cache on the next `get_cache()`."""
    global _cache
    _cache = None
