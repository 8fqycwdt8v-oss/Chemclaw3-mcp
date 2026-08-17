"""Deterministic predictor doubles, shipped with the engine rather than hidden in the tests.

They live here, in the package, for two reasons. A test-only class in `tests/` cannot be used by
anything else, and the thing that most wants one is *an operator* — `CHEMCLAW_RXNPREDICT_ENABLED_
FORWARD_MODELS=fake_a` gives a running server with a working tool surface and no model weights,
which is exactly what a deployment rehearsal or a Chemclaw3 integration test wants.

They are never registered automatically. `discover_predictors()` does not import this module, so a
double only exists when someone asks for one *by name* — a fake predictor that could appear in a
production ensemble by accident would be far worse than no fake at all.

Asking by name is what `register_requested()` below does, and until it existed the operator half of
the paragraph above was not true: only `tests/conftest.py` ever constructed a double, so setting
`CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS=fake_a` against a real uvicorn registered nothing at
all and the server came up with an empty tool surface. The env var was documented, inert, and
silent about it.
"""

from __future__ import annotations

from collections.abc import Callable

from chemclaw_mcp_rxnpredict.engine.config import Settings, get_settings
from chemclaw_mcp_rxnpredict.engine.predictors import (
    list_conditions,
    list_forward,
    register_conditions,
    register_forward,
)
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


# The catalogue of doubles an operator may ask for, by exact name. Factories rather than instances
# so that two registrations never share one object, and so that nothing is constructed unless it is
# actually requested.
_FORWARD_DOUBLES: dict[str, Callable[[], BaseForwardPredictor]] = {
    "fake_a": lambda: FakeForwardPredictor("fake_a", ["CC(=O)Nc1ccccc1", "CCOC(C)=O"]),
    "fake_b": lambda: FakeForwardPredictor("fake_b", ["CC(=O)Nc1ccccc1", "CC(=O)OC(C)=O"]),
}

_CONDITIONS_DOUBLES: dict[str, Callable[[], BaseConditionsPredictor]] = {
    "fake_c": lambda: FakeConditionsPredictor(
        "fake_c", catalysts=["Pd(OAc)2"], solvents=["THF"], temperature=25.0
    ),
    "fake_d": lambda: FakeConditionsPredictor(
        "fake_d", catalysts=["Pd(OAc)2"], solvents=["THF"], temperature=28.0
    ),
}


def register_requested(settings: Settings | None = None) -> list[str]:
    """Register exactly the doubles the enabled-model settings name, and return their names.

    This is the operator-facing half of this module: it is what makes
    `CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS=fake_a` mean something against a real server
    instead of being a documented no-op.

    Two properties are deliberate and load-bearing:

    * **Never on `"*"`.** `parse_enabled` returns `None` for `*` and for empty, which is this
      module's "registered by accident" case and the one its header forbids. A `None` list
      registers nothing, so the default configuration cannot grow a fake predictor.
    * **Exact names only.** A name that is not a known double is ignored here rather than raising,
      because the enabled list legitimately names real predictors too — it is a filter over
      everything available, not a list of doubles.

    Already-registered names are skipped, so calling this after `discover_predictors()` cannot
    collide with a real predictor that happens to share a name, and calling it twice is harmless.

    Args:
        settings: Settings to read; the process-wide settings when omitted.

    Returns:
        The names actually registered, in the order they were registered — empty when the
        configuration asked for no doubles, which is the normal production case.
    """
    resolved = settings if settings is not None else get_settings()
    known = _known_names()
    registered: list[str] = []

    # Two explicit blocks rather than one loop over (catalogue, register) pairs: the two registries
    # take different predictor types, and looping over both erases that into a union which the
    # registration functions rightly reject.
    forward = resolved.parse_enabled(resolved.enabled_forward_models)
    if forward is not None:
        for name in sorted(forward & (_FORWARD_DOUBLES.keys() - known)):
            register_forward(_FORWARD_DOUBLES[name]())
            registered.append(name)

    conditions = resolved.parse_enabled(resolved.enabled_conditions_models)
    if conditions is not None:
        for name in sorted(conditions & (_CONDITIONS_DOUBLES.keys() - known)):
            register_conditions(_CONDITIONS_DOUBLES[name]())
            registered.append(name)

    return registered


def _known_names() -> set[str]:
    """Every predictor name already in either registry."""
    return {p.name for p in list_forward()} | {p.name for p in list_conditions()}
