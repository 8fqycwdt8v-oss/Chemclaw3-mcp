"""The prediction cache: bounded, in-process, and deliberately not on disk.

The image is rootless with a read-only root filesystem, and a memo of a deterministic function is
not worth a volume. So it is an in-process LRU: a restart loses it (the win is repeated calls within
a conversation), there is no TTL (a prediction goes stale only with a new image), and there is no
clear tool, so the server has no state-changing surface.

The key is predictor, canonical reactants (and product), and `top_k`, so two spellings of one
reaction share an entry. A caller derives the key once (`key_forward`, `key_conditions`) and passes
it to `get` and `set`, so a miss canonicalises once and a broken canonicaliser is counted once. An
input this server will not canonicalise gets a `None` key and is simply not cached: raw caller text
is not an identity, and keying on it would also absorb the size-bound refusals and hide a broken
component.
"""

from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from mcp_server_kit import degradation
from mcp_server_kit.limits import echo

from chemclaw_mcp_rxnpredict.engine.predictors import SERVER
from chemclaw_mcp_rxnpredict.engine.preprocessing import (
    canonical_multi_smiles,
    canonical_smiles,
)

logger = logging.getLogger(__name__)

Payload = list[dict[str, Any]]

# The component a degraded answer from here is labelled with: "this pod cannot derive a cache
# identity". Registered at import, since `degradation.record` clamps unregistered components to
# `<unknown>`.
COMPONENT = "prediction_cache"
degradation.register_components(COMPONENT)


def _hash_key(parts: list[str]) -> str:
    """A stable digest of the key parts, so one string keys the entry however long the SMILES."""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _canonical_or_none(canonicalise: Callable[[str], str], smiles: str) -> str | None:
    """The canonical form of `smiles`, or `None` when this server will not derive one.

    - `ValueError` (unparseable, or over the `mcp_server_kit.limits` bounds) is the caller's input:
      logged at DEBUG with the string truncated, and not counted.
    - Anything else is this pod (RDKit absent, `EgressForbidden`, allocation failure): classified
      and counted on `chemclaw_mcp_degraded_total`.

    Args:
        canonicalise: `canonical_multi_smiles` or `canonical_smiles`.
        smiles: The caller's string, exactly as it arrived.

    Returns:
        The canonical form, or `None` if there is not one to be had.
    """
    try:
        return canonicalise(smiles)
    except ValueError as exc:
        logger.debug("not caching %s: %s", echo(smiles), exc)
        return None
    # BLE001: blind on purpose and classified on the next line; `ValueError` is handled above.
    except Exception as exc:  # noqa: BLE001
        cause = degradation.classify(exc)
        degradation.record(server=SERVER, component=COMPONENT, cause=cause)
        logger.warning(
            "the prediction cache cannot canonicalise a key [%s]: %r; this call is not cached",
            cause,
            exc,
        )
        return None


class PredictionCache:
    """A bounded LRU over predictor results. Disabled, it is a no-op with the same interface."""

    def __init__(self, *, enabled: bool, max_entries: int) -> None:
        """Hold at most `max_entries` results, evicting least-recently-used first."""
        self.enabled = enabled and max_entries > 0
        self._max_entries = max_entries
        self._entries: OrderedDict[str, Payload] = OrderedDict()

    def get(self, key: str | None) -> Payload | None:
        """Fetch and mark as most-recently-used; a `None` key is uncacheable and always misses."""
        if not self.enabled or key is None:
            return None
        found = self._entries.get(key)
        if found is None:
            return None
        self._entries.move_to_end(key)
        return found

    def set(self, key: str | None, payload: Payload) -> None:
        """Store, evicting the least-recently-used entry once the bound is passed.

        A `None` key is an uncacheable input and is dropped rather than stored under a guess.
        """
        if not self.enabled or key is None:
            return
        self._entries[key] = payload
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def key_forward(self, model_name: str, reactants: str, top_k: int) -> str | None:
        """Key for a forward prediction, or `None` when the reactants have no canonical form."""
        canon = _canonical_or_none(canonical_multi_smiles, reactants)
        if canon is None:
            return None
        return _hash_key(["fwd", model_name, canon, str(top_k)])

    def key_conditions(
        self, model_name: str, reactants: str, product: str, top_k: int
    ) -> str | None:
        """Key for a conditions prediction, or `None` if either side has no canonical form.

        Short-circuits on the reactants so one broken canonicaliser counts once per answer.
        """
        canon_reactants = _canonical_or_none(canonical_multi_smiles, reactants)
        if canon_reactants is None:
            return None
        canon_product = _canonical_or_none(canonical_smiles, product)
        if canon_product is None:
            return None
        return _hash_key(["cond", model_name, canon_reactants, canon_product, str(top_k)])

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
