"""Predictor abstract base classes.

The base class serves and fills the in-process prediction cache when enabled; subclasses implement
`predict_sync` and `load`.
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
        # The lazy load must happen once, not once per coroutine that arrived first: concurrent
        # loads of one checkpoint can OOM the pod and rebind the model under a running prediction.
        # Safe to construct at import, since an `asyncio.Lock` binds no loop at construction.
        self._load_lock = asyncio.Lock()

    async def ensure_loaded(self) -> None:
        """Load once, however many callers arrive before the first load finishes."""
        if self._loaded:
            return
        async with self._load_lock:
            if not self._loaded:
                await asyncio.to_thread(self.load)
                self._loaded = True

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

        Served from and stored to the prediction cache when it is enabled.
        """
        from ..cache import get_cache  # local import to avoid early settings load

        cache = get_cache()
        # One key derivation per prediction, so a broken canonicaliser is counted once.
        key = cache.key_forward(self.name, reactants, top_k)
        cached = cache.get(key)
        if cached is not None:
            return [ForwardPrediction.model_validate(d) for d in cached]

        await self.ensure_loaded()
        result = await asyncio.to_thread(self.predict_sync, reactants, top_k)

        # Only cache non-empty results: an empty list is usually a transient soft failure, and
        # caching it would drop the predictor from the ensemble until the entry is evicted.
        if result:
            cache.set(key, [p.model_dump() for p in result])
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
        # One derivation per prediction; see BaseForwardPredictor.predict.
        key = cache.key_conditions(self.name, reactants, product, top_k)
        cached = cache.get(key)
        if cached is not None:
            return [ConditionsPrediction.model_validate(d) for d in cached]

        await self.ensure_loaded()
        result = await asyncio.to_thread(self.predict_sync, reactants, product, top_k)

        # See BaseForwardPredictor.predict: don't cache empty (likely-transient) results.
        if result:
            cache.set(key, [p.model_dump() for p in result])
        return result
