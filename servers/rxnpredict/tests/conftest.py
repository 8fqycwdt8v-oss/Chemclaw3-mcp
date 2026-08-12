"""Deterministic stand-in predictors, so the ensemble path is tested without a GPU or a checkpoint.

The predictors this server ships are large: `reaction_t5_v2` is a T5 checkpoint and `rxn_insight`
pulls a transformer of its own. A test suite that needed them would run nowhere and would be
skipped everywhere, which is how the interesting half of a server ends up untested — upstream's own
smoke test settles for asserting the endpoint returns "200 or 503".

So the *aggregation* is tested against fakes that return fixed, known predictions. That is the part
this fork can break: the models are third-party and their weights are frozen, while the voting,
the class gating, the selection rules and the tool surface are ours. A fake predictor is the right
double here precisely because a real one adds no information about any of them.
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

    The two forward doubles agree on one product and disagree on the rest, which is the case the
    consensus exists to resolve — a test where every model returns the same thing would pass under
    an aggregator that ignored its inputs entirely.
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
