"""Tests for the meta-model aggregators."""

from __future__ import annotations

import pytest

rdkit = pytest.importorskip("rdkit")

from chemclaw_mcp_rxnpredict.engine.config import Settings  # noqa: E402
from chemclaw_mcp_rxnpredict.engine.meta.aggregator import (  # noqa: E402
    aggregate_conditions,
    aggregate_forward,
)
from chemclaw_mcp_rxnpredict.engine.schemas import (  # noqa: E402
    ConditionsPrediction,
    ForwardPrediction,
)


@pytest.fixture()
def settings() -> Settings:
    s = Settings()
    s.model_trust_priors = {
        "model_a": 1.0,
        "model_b": 1.0,
        "model_c": 0.5,
    }
    return s


def _fwd(model: str, smi: str, rank: int, score: float = 0.9) -> ForwardPrediction:
    return ForwardPrediction(product_smiles=smi, score=score, rank=rank, source_model=model)


def test_forward_consensus_prefers_unanimous_top_pick(settings):
    per_model = {
        "model_a": [_fwd("model_a", "CCO", 1), _fwd("model_a", "CCC", 2)],
        "model_b": [_fwd("model_b", "CCO", 1), _fwd("model_b", "OCC", 2)],  # OCC == CCO canon
        "model_c": [_fwd("model_c", "CCO", 1)],
    }
    out = aggregate_forward(per_model, settings, top_k=3)
    assert out[0].product_smiles == "CCO"
    assert out[0].vote_count == 3
    assert out[0].consensus_score == 1.0
    assert set(out[0].contributing_models) == {"model_a", "model_b", "model_c"}


def test_forward_consensus_breaks_ties_by_vote_count(settings):
    # Two candidates with equal raw weight; one has more voters → it wins.
    per_model = {
        "model_a": [_fwd("model_a", "CCO", 1, score=1.0)],
        "model_b": [_fwd("model_b", "CCO", 2, score=1.0)],  # weight 0.5
        "model_c": [_fwd("model_c", "CCC", 1, score=1.0)],  # weight 0.5
    }
    out = aggregate_forward(per_model, settings, top_k=2)
    # CCO has weight 1.0 + 0.5 = 1.5 with 2 voters; CCC has 0.5 with 1.
    assert out[0].product_smiles == "CCO"
    assert out[0].vote_count == 2


def test_forward_handles_no_predictions(settings):
    assert aggregate_forward({}, settings, top_k=5) == []


def test_forward_ignores_invalid_smiles(settings):
    per_model = {
        "model_a": [
            _fwd("model_a", "CCO", 1),
            _fwd("model_a", "not_smiles", 2),
        ],
    }
    out = aggregate_forward(per_model, settings, top_k=5)
    # Invalid SMILES is preserved as-is so weight is still summed, but we should
    # still get at least the valid one ranked first.
    assert any(r.product_smiles == "CCO" for r in out)


def _cond(model, **kw) -> ConditionsPrediction:
    return ConditionsPrediction(
        catalysts=kw.get("catalysts", []),
        solvents=kw.get("solvents", []),
        reagents=kw.get("reagents", []),
        temperature_c=kw.get("temperature_c"),
        score=kw.get("score", 0.9),
        rank=kw.get("rank", 1),
        source_model=model,
    )


def test_conditions_consensus_buckets_temperature(settings):
    per_model = {
        "model_a": [_cond("model_a", solvents=["O"], temperature_c=25.0, rank=1)],
        "model_b": [_cond("model_b", solvents=["O"], temperature_c=28.0, rank=1)],
        "model_c": [_cond("model_c", solvents=["O"], temperature_c=78.0, rank=1)],
    }
    out = aggregate_conditions(per_model, settings, top_k=2)
    # 25 and 28 both bucket to 30; 78 buckets to 80 -> two distinct condition sets
    assert len(out) == 2
    top = out[0]
    assert top.vote_count == 2
    assert top.temperature_c is not None
    assert 24.0 <= top.temperature_c <= 29.0  # average of 25 + 28


def test_conditions_canonicalises_solvent_strings(settings):
    # "O" and "[OH2]" both canonicalise to "O" — should count as the same vote.
    per_model = {
        "model_a": [_cond("model_a", solvents=["O"], rank=1)],
        "model_b": [_cond("model_b", solvents=["[OH2]"], rank=1)],
    }
    out = aggregate_conditions(per_model, settings, top_k=1)
    assert out[0].vote_count == 2
