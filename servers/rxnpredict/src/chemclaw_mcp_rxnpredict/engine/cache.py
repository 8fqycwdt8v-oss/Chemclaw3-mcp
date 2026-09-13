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
two spellings of one reaction share a slot.

**An input this server will not canonicalise is not cached, and it used to be keyed by the caller's
raw text.** That fallback was `except Exception: return smiles`, and it was wrong three ways, each
measured on the shipped code before it was replaced:

- **It was not a key.** `canonical_multi_smiles` sorts the components, raw text does not, so
  `CCO.<garbage>` and `<garbage>.CCO` — one set of molecules, two spellings — minted two entries.
  A key derived from text nothing validated is not an identity, and this repository's rule for that
  case is `CLAUDE.md`'s: refuse rather than approximate. So the key derivation returns `None` and
  the entry is simply not cached; `get` misses, `set` is a no-op, and the prediction runs. A cache
  still never fails a prediction — it declines to claim it recognised one.
- **It swallowed two deliberate refusals.** `mcp_server_kit.limits` exists because `MolToSmiles`
  on a large enough molecule overflows the C stack and takes the pod down; `canonical_smiles`
  raises *before* parsing for that reason. Both bounds arrived here as a `ValueError` and became a
  cache row keyed by the 5,000-character string the bound exists to reject.
- **It hid a broken component.** The bare `except Exception` caught `ImportError` (RDKit absent
  from the image), `EgressForbidden` (the guard refusing a library's outbound call, which is an
  `OSError` and so looks like nothing in particular) and `MemoryError` alike — measured, all three
  returned raw text and moved no counter. `CLAUDE.md` claims every path in this fleet that catches
  an exception and answers anyway classifies it through `mcp_server_kit.degradation`; this one did
  not. It does now, and only for that arm: a `ValueError` is the caller's input being outside what
  this server canonicalises, which is not a degradation of this pod and must not fire the metric
  that says one component of it has gone missing.
"""

from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from mcp_server_kit import degradation

from chemclaw_mcp_rxnpredict.engine.predictors import SERVER
from chemclaw_mcp_rxnpredict.engine.preprocessing import (
    canonical_multi_smiles,
    canonical_smiles,
    truncate_echo,
)

logger = logging.getLogger(__name__)

Payload = list[dict[str, Any]]

# What a degraded answer from here is labelled. One name for both canonicalisers, because what the
# scrape needs to say is "this pod cannot derive a cache identity", not which of the two calls hit
# it. Declared through `register_components` at import, since `degradation.record` clamps an
# unregistered component onto `<unknown>` rather than minting a series for it.
COMPONENT = "prediction_cache"
degradation.register_components(COMPONENT)


def _hash_key(parts: list[str]) -> str:
    """A stable digest of the key parts, so one string keys the entry however long the SMILES."""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _canonical_or_none(canonicalise: Callable[[str], str], smiles: str) -> str | None:
    """The canonical form of `smiles`, or `None` when this server will not derive one.

    `None` is the whole fallback: the caller turns it into "do not cache this", because the
    alternative — keying on unvalidated caller text — is an identity nobody checked. See the module
    docstring for the three defects that produced.

    The two arms are different events and are answered differently:

    - **`ValueError`** is what `preprocessing` raises for an input outside what this server
      canonicalises — an unparseable SMILES, or one over `mcp_server_kit.limits`' character or atom
      bound. That is a fact about the caller's argument, not about this pod, so it is logged at
      DEBUG (with the string truncated, since an over-length one is exactly the case that reaches
      here) and moves no metric. Counting it would make `chemclaw_mcp_degraded_total` — the series
      that means a component of this server has gone missing — fire on every typo.
    - **Anything else** is this pod: RDKit absent from the image (`ImportError`), the egress guard
      refusing a library's outbound call (`EgressForbidden`, an `OSError` that any coarse handler
      buries), an allocation failing. Those are classified and counted, which is what makes them
      visible from a scrape rather than from a log line nobody tails.

    Args:
        canonicalise: `canonical_multi_smiles` or `canonical_smiles`.
        smiles: The caller's string, exactly as it arrived.

    Returns:
        The canonical form, or `None` if there is not one to be had.
    """
    try:
        return canonicalise(smiles)
    except ValueError as exc:
        logger.debug("not caching %s: %s", truncate_echo(smiles), exc)
        return None
    except Exception as exc:
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

    def _get(self, key: str | None) -> Payload | None:
        """Fetch and mark as most-recently-used; a `None` key is uncacheable and always misses."""
        if not self.enabled or key is None:
            return None
        found = self._entries.get(key)
        if found is None:
            return None
        self._entries.move_to_end(key)
        return found

    def _set(self, key: str | None, payload: Payload) -> None:
        """Store, evicting the least-recently-used entry once the bound is passed.

        A `None` key is an uncacheable input and is dropped rather than stored under a guess.
        """
        if not self.enabled or key is None:
            return
        self._entries[key] = payload
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def _key_forward(self, model_name: str, reactants: str, top_k: int) -> str | None:
        """Key for a forward prediction, or `None` when the reactants have no canonical form."""
        canon = _canonical_or_none(canonical_multi_smiles, reactants)
        if canon is None:
            return None
        return _hash_key(["fwd", model_name, canon, str(top_k)])

    def _key_conditions(
        self, model_name: str, reactants: str, product: str, top_k: int
    ) -> str | None:
        """Key for a conditions prediction, or `None` if either side has no canonical form."""
        canon_reactants = _canonical_or_none(canonical_multi_smiles, reactants)
        canon_product = _canonical_or_none(canonical_smiles, product)
        if canon_reactants is None or canon_product is None:
            return None
        return _hash_key(["cond", model_name, canon_reactants, canon_product, str(top_k)])

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
