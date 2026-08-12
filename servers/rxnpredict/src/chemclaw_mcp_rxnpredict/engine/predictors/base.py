"""Predictor abstract base classes.

Caching is transparent: when the prediction cache is enabled the base class serves cached results
and writes new ones on a miss. Subclasses only need to implement `predict_sync` (and `load`).

The cache is the bounded in-process LRU in `cache.py` — upstream's `diskcache` did not survive the
fork, because a rootless read-only image has nowhere to write it. It follows that there is no TTL
and no persistence across a restart; `cache.py` argues why neither is worth a volume.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

from ..schemas import ConditionsPrediction, ForwardPrediction

logger = logging.getLogger(__name__)


class BasePredictor(ABC):
    """Shared metadata for forward and conditions predictors."""

    name: str
    description: str
    citation: str | None = None
    extras_install: str | None = None

    def __init__(self) -> None:
        self._loaded = False

    @abstractmethod
    def load(self) -> None:
        """Load model weights / open files. Called lazily on first predict()."""

    def is_loaded(self) -> bool:
        return self._loaded


class BaseForwardPredictor(BasePredictor):
    """Given reactants (and optional agents) SMILES, predict product SMILES."""

    @abstractmethod
    def predict_sync(self, reactants: str, top_k: int) -> list[ForwardPrediction]:
        """Synchronous prediction. Override this in subclasses."""

    async def predict(self, reactants: str, top_k: int) -> list[ForwardPrediction]:
        """Async wrapper; offloads sync inference to a worker thread.

        Reads and writes the in-process prediction cache when it is enabled. Both cache operations
        stay on the event loop — only `load` and `predict_sync` go to a thread — so the LRU needs no
        lock of its own.
        """
        from ..cache import get_cache  # local import to avoid early settings load

        cache = get_cache()
        cached = cache.get_forward(self.name, reactants, top_k)
        if cached is not None:
            return [ForwardPrediction.model_validate(d) for d in cached]

        if not self._loaded:
            await asyncio.to_thread(self.load)
            self._loaded = True
        result = await asyncio.to_thread(self.predict_sync, reactants, top_k)

        # Only cache non-empty results: an empty list usually signals a transient soft failure
        # rather than an answer, and this cache has no TTL to expire it — so caching one would drop
        # the predictor out of the ensemble for the rest of the process's life.
        if result:
            cache.set_forward(self.name, reactants, top_k, [p.model_dump() for p in result])
        return result


class BaseConditionsPredictor(BasePredictor):
    """Given reactants + product SMILES, predict reaction conditions."""

    @abstractmethod
    def predict_sync(
        self, reactants: str, product: str, top_k: int
    ) -> list[ConditionsPrediction]: ...

    async def predict(self, reactants: str, product: str, top_k: int) -> list[ConditionsPrediction]:
        from ..cache import get_cache

        cache = get_cache()
        cached = cache.get_conditions(self.name, reactants, product, top_k)
        if cached is not None:
            return [ConditionsPrediction.model_validate(d) for d in cached]

        if not self._loaded:
            await asyncio.to_thread(self.load)
            self._loaded = True
        result = await asyncio.to_thread(self.predict_sync, reactants, product, top_k)

        # See BaseForwardPredictor.predict: don't cache empty (likely-transient) results.
        if result:
            cache.set_conditions(
                self.name, reactants, product, top_k, [p.model_dump() for p in result]
            )
        return result
