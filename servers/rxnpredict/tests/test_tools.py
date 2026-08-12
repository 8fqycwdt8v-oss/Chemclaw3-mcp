"""What the tools answer, and what they refuse to answer.

The aggregation is what this fork can break — the model weights are third-party and frozen, while
the voting, the class gating, the predictor selection and the tool surface are ours. So these tests
drive the full ensemble path through the MCP tool functions with deterministic doubles.
"""

from __future__ import annotations

import pytest
from chemclaw_mcp_rxnpredict import tools
from chemclaw_mcp_rxnpredict.engine import predictors as registry


async def test_the_consensus_ranks_the_agreed_product_first(fake_predictors: None) -> None:
    """Two models, one shared product and one each of their own. Agreement must win."""
    result = await tools.predict_forward_reaction("CC(=O)Cl.Nc1ccccc1", top_k=3)
    assert result.consensus[0].product_smiles == "CC(=O)Nc1ccccc1"
    assert result.consensus[0].vote_count == 2
    assert sorted(result.consensus[0].contributing_models) == ["fake_a", "fake_b"]
    assert result.n_models_queried == 2
    assert result.n_models_succeeded == 2


async def test_every_model_s_own_output_comes_back_beside_the_consensus(
    fake_predictors: None,
) -> None:
    """The spread is information: a caller must be able to see who disagreed, and about what."""
    result = await tools.predict_forward_reaction("CC(=O)Cl.Nc1ccccc1", top_k=3)
    assert set(result.per_model) == {"fake_a", "fake_b"}
    assert [p.product_smiles for p in result.per_model["fake_a"]] == [
        "CC(=O)Nc1ccccc1",
        "CCOC(C)=O",
    ]


async def test_the_answer_carries_its_own_provenance(fake_predictors: None) -> None:
    """A prediction without who produced it and under what weighting is not quotable."""
    result = await tools.predict_forward_reaction("CC(=O)Cl.Nc1ccccc1")
    assert "fake_a" in result.source and "fake_b" in result.source
    assert "trust priors" in result.source


async def test_a_failing_predictor_costs_its_vote_and_not_the_answer(
    fake_predictors: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One model falling over must degrade the ensemble, never fail the call — and must show."""

    async def explode(*_args: object, **_kwargs: object) -> list[object]:
        raise RuntimeError("checkpoint is corrupt")

    monkeypatch.setattr(registry.get_forward("fake_b"), "predict", explode)
    result = await tools.predict_forward_reaction("CC(=O)Cl.Nc1ccccc1")
    assert result.n_models_queried == 2
    assert result.n_models_succeeded == 1
    assert set(result.per_model) == {"fake_a"}


async def test_conditions_vote_on_the_whole_set_with_temperature_bucketed(
    fake_predictors: None,
) -> None:
    """25 °C and 28 °C are the same suggestion; splitting them would drown out the agreement."""
    result = await tools.predict_reaction_conditions(
        "CC(=O)Cl.Nc1ccccc1", "CC(=O)Nc1ccccc1", top_k=3
    )
    assert result.consensus[0].vote_count == 2
    assert result.consensus[0].solvents == ["THF"]
    assert result.n_models_succeeded == 2


async def test_a_named_subset_narrows_the_ensemble(fake_predictors: None) -> None:
    """`models` is how a caller interrogates one predictor without leaving the consensus tool."""
    result = await tools.predict_forward_reaction("CC(=O)Cl.Nc1ccccc1", models=["fake_a"])
    assert result.n_models_queried == 1
    assert set(result.per_model) == {"fake_a"}


async def test_a_disabled_predictor_is_not_queried(
    fake_predictors: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configuration wins over the caller: a predictor an operator turned off stays off."""
    monkeypatch.setenv("CHEMCLAW_RXNPREDICT_DISABLED_MODELS", "fake_b")
    from chemclaw_mcp_rxnpredict.engine.config import reset_settings_for_tests

    reset_settings_for_tests()
    result = await tools.predict_forward_reaction("CC(=O)Cl.Nc1ccccc1", models=["fake_a", "fake_b"])
    assert set(result.per_model) == {"fake_a"}


async def test_a_single_model_call_reaches_that_model(fake_predictors: None) -> None:
    """The introspection path, for when the question is about a model rather than a reaction."""
    predictions = await tools.predict_forward_single_model("fake_a", "CC(=O)Cl.Nc1ccccc1", top_k=1)
    assert [p.product_smiles for p in predictions] == ["CC(=O)Nc1ccccc1"]


async def test_an_unknown_model_names_the_ones_that_are_loaded(fake_predictors: None) -> None:
    """The error has to make the next call possible, not just report failure."""
    with pytest.raises(ValueError, match="fake_a"):
        await tools.predict_forward_single_model("no_such_model", "CCO")


async def test_a_deployment_with_no_predictors_says_so_plainly() -> None:
    """The realistic misconfiguration: extras uninstalled, so nothing registered."""
    saved = dict(registry._FORWARD)
    registry._FORWARD.clear()
    try:
        with pytest.raises(ValueError, match="list_available_models"):
            await tools.predict_forward_reaction("CCO")
    finally:
        registry._FORWARD.update(saved)


def test_unavailable_predictors_are_reported_with_their_reason() -> None:
    """A consensus of one, in a deployment expecting five, is only visible through this tool."""
    listed = tools.list_available_models()
    names = {row.name for row in listed.forward + listed.conditions}
    assert "reaction_t5_v2" in names
    for row in listed.forward + listed.conditions:
        if not row.available:
            assert row.unavailable_reason, row.name


def test_classify_reaction_names_a_common_class() -> None:
    """Acid chloride plus amine is an amide formation, and the gating depends on saying so."""
    result = tools.classify_reaction("CC(=O)Cl.Nc1ccccc1", product="CC(=O)Nc1ccccc1")
    assert result.reaction_class == "amide_formation"
    assert result.canonical_product == "CC(=O)Nc1ccccc1"
    assert "coarse" in result.source


def test_classify_reaction_admits_when_no_rule_matches() -> None:
    """`other` must mean "no rule matched" and must not be dressed up as a classification."""
    assert tools.classify_reaction("[Xe]").reaction_class == "other"


async def test_a_repeated_call_is_served_from_the_cache(fake_predictors: None) -> None:
    """The win the cache exists for: the same question twice inside one conversation."""
    from chemclaw_mcp_rxnpredict.engine.cache import get_cache

    await tools.predict_forward_reaction("CC(=O)Cl.Nc1ccccc1", top_k=2)
    cached = get_cache().get_forward("fake_a", "CC(=O)Cl.Nc1ccccc1", 2)
    assert cached is not None
    assert cached[0]["product_smiles"] == "CC(=O)Nc1ccccc1"
