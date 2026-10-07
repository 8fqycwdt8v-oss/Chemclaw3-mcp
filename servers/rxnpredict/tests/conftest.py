"""Deterministic stand-in predictors, so the ensemble path is tested without a GPU or a checkpoint.

The real predictors are large third-party models with frozen weights; what this fork can break
is the voting, class gating, selection rules and tool surface. Fakes with fixed, known
predictions test exactly that, and a real model would add no information about any of it.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from chemclaw_mcp_rxnpredict.engine import predictors as registry
from chemclaw_mcp_rxnpredict.engine.base_doubles import (
    FakeConditionsPredictor,
    FakeForwardPredictor,
)
from chemclaw_mcp_rxnpredict.engine.cache import reset_cache_for_tests
from chemclaw_mcp_rxnpredict.engine.config import reset_settings_for_tests


@pytest.fixture
def fake_predictors() -> Iterator[None]:
    """Register two forward and two condition doubles, and restore the registry afterwards.

    The forward doubles agree on one product and disagree on the rest, so an aggregator that ignored
    its inputs would fail.
    """
    saved_forward = dict(registry._FORWARD)
    saved_conditions = dict(registry._CONDITIONS)
    reset_cache_for_tests()
    reset_settings_for_tests()

    registry._FORWARD.clear()
    registry._CONDITIONS.clear()
    registry.register_forward(FakeForwardPredictor("fake_a", ["CC(=O)Nc1ccccc1", "CCOC(C)=O"]))
    registry.register_forward(FakeForwardPredictor("fake_b", ["CC(=O)Nc1ccccc1", "CC(=O)OC(C)=O"]))
    registry.register_conditions(
        FakeConditionsPredictor(
            "fake_c", catalysts=["Pd(OAc)2"], solvents=["THF"], temperature=25.0
        )
    )
    registry.register_conditions(
        FakeConditionsPredictor(
            "fake_d", catalysts=["Pd(OAc)2"], solvents=["THF"], temperature=28.0
        )
    )
    try:
        yield
    finally:
        registry._FORWARD.clear()
        registry._FORWARD.update(saved_forward)
        registry._CONDITIONS.clear()
        registry._CONDITIONS.update(saved_conditions)
        reset_cache_for_tests()
        reset_settings_for_tests()
