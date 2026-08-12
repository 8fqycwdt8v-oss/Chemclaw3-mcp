"""Deterministic predictor doubles, shipped with the engine rather than hidden in the tests.

They live here, in the package, for two reasons. A test-only class in `tests/` cannot be used by
anything else, and the thing that most wants one is *an operator* — `CHEMCLAW_RXNPREDICT_ENABLED_
FORWARD_MODELS=fake_a` gives a running server with a working tool surface and no model weights,
which is exactly what a deployment rehearsal or a Chemclaw3 integration test wants.

They are never registered automatically. `discover_predictors()` does not import this module, so a
double only exists when a test or a fixture asks for one — a fake predictor that could appear in a
production ensemble by accident would be far worse than no fake at all.
"""

from __future__ import annotations

from chemclaw_mcp_rxnpredict.engine.predictors.base import (
    BaseConditionsPredictor,
    BaseForwardPredictor,
)
from chemclaw_mcp_rxnpredict.engine.schemas import ConditionsPrediction, ForwardPrediction


class FakeForwardPredictor(BaseForwardPredictor):
    """Returns a fixed product list, ranked, with scores decaying by rank."""

    def __init__(self, name: str, products: list[str]) -> None:
        """Name it and fix what it will always predict."""
        super().__init__()
        self.name = name
        self.description = f"Deterministic double returning {len(products)} fixed product(s)."
        self.citation = None
        self.extras_install = None
        self._products = products

    def load(self) -> None:
        """Nothing to load — which is the point."""
        self._loaded = True

    def predict_sync(self, reactants: str, top_k: int) -> list[ForwardPrediction]:
        """The fixed products, truncated to `top_k`, scored 1.0, 0.9, 0.8 ..."""
        return [
            ForwardPrediction(
                product_smiles=smiles,
                score=max(0.0, 1.0 - 0.1 * index),
                rank=index + 1,
                source_model=self.name,
            )
            for index, smiles in enumerate(self._products[:top_k])
        ]


class FakeConditionsPredictor(BaseConditionsPredictor):
    """Returns one fixed condition set."""

    def __init__(
        self,
        name: str,
        *,
        catalysts: list[str] | None = None,
        solvents: list[str] | None = None,
        reagents: list[str] | None = None,
        temperature: float | None = None,
    ) -> None:
        """Name it and fix the condition set it will always suggest."""
        super().__init__()
        self.name = name
        self.description = "Deterministic double returning one fixed condition set."
        self.citation = None
        self.extras_install = None
        self._catalysts = catalysts or []
        self._solvents = solvents or []
        self._reagents = reagents or []
        self._temperature = temperature

    def load(self) -> None:
        """Nothing to load."""
        self._loaded = True

    def predict_sync(self, reactants: str, product: str, top_k: int) -> list[ConditionsPrediction]:
        """The one fixed condition set, at rank 1."""
        return [
            ConditionsPrediction(
                catalysts=self._catalysts,
                solvents=self._solvents,
                reagents=self._reagents,
                temperature_c=self._temperature,
                score=1.0,
                rank=1,
                source_model=self.name,
            )
        ]
