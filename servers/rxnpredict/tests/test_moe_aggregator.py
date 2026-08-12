"""Tests for per-class (MoE) gating in the aggregator."""

from __future__ import annotations

import pytest

pytest.importorskip("rdkit")

from chemclaw_mcp_rxnpredict.engine.config import Settings
from chemclaw_mcp_rxnpredict.engine.meta.aggregator import aggregate_forward
from chemclaw_mcp_rxnpredict.engine.schemas import ForwardPrediction


def _fwd(model: str, smi: str, rank: int = 1, score: float = 1.0) -> ForwardPrediction:
    return ForwardPrediction(product_smiles=smi, score=score, rank=rank, source_model=model)


@pytest.fixture()
def settings() -> Settings:
    s = Settings()
    s.use_class_priors = True
    # Global priors say model_a > model_b
    s.model_trust_priors = {"model_a": 1.0, "model_b": 0.1}
    # But per-class priors for amide formation flip the trust
    s.model_trust_priors_by_class = {
        "amide_formation": {"model_a": 0.1, "model_b": 1.0},
    }
    return s


def test_per_class_priors_override_global_for_known_class(settings):
    """For an amide-formation reaction, model_b's vote should outweigh model_a's
    because per-class priors flip the trust."""
    per_model = {
        "model_a": [_fwd("model_a", "CCO", rank=1)],  # globally trusted, but
        "model_b": [_fwd("model_b", "CC(=O)Nc1ccccc1", rank=1)],
    }
    # Acetyl chloride + aniline -> amide formation
    out = aggregate_forward(per_model, settings, top_k=2, reactants="CC(=O)Cl.Nc1ccccc1")
    assert out[0].product_smiles == "CC(=O)Nc1ccccc1"


def test_global_priors_used_when_class_unknown(settings):
    """For a reaction that doesn't match any class rule, global priors apply."""
    per_model = {
        "model_a": [_fwd("model_a", "CCO", rank=1)],
        "model_b": [_fwd("model_b", "CCC", rank=1)],
    }
    out = aggregate_forward(
        per_model,
        settings,
        top_k=2,
        reactants="C(F)(F)F.C#N",  # no rule matches
    )
    assert out[0].product_smiles == "CCO"


def test_class_priors_disabled_uses_globals(settings):
    """When use_class_priors=False, even amide-forming reactants use global priors."""
    settings.use_class_priors = False
    per_model = {
        "model_a": [_fwd("model_a", "CCO", rank=1)],
        "model_b": [_fwd("model_b", "CC(=O)Nc1ccccc1", rank=1)],
    }
    out = aggregate_forward(per_model, settings, top_k=2, reactants="CC(=O)Cl.Nc1ccccc1")
    # With class priors off, model_a (global prior 1.0) beats model_b (0.1)
    assert out[0].product_smiles == "CCO"


def test_no_reactants_falls_back_to_globals(settings):
    """Aggregator without `reactants=` ignores class priors entirely."""
    per_model = {
        "model_a": [_fwd("model_a", "CCO", rank=1)],
        "model_b": [_fwd("model_b", "CC(=O)Nc1ccccc1", rank=1)],
    }
    out = aggregate_forward(per_model, settings, top_k=2)
    assert out[0].product_smiles == "CCO"
