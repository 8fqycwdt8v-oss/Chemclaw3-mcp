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


async def test_the_top_consensus_score_measures_agreement_rather_than_being_one_by_definition(
    fake_predictors: None,
) -> None:
    """A confidence that is always maximal measures nothing, and this one always was.

    The score was `weight / max_weight` over the sorted candidates, so rank 1's weight *was*
    `max_weight` and rank 1 scored exactly 1.0 — for a five-model unanimous vote and for one model
    that guessed a product at a per-token probability of 1e-4 alike. The denominator has to be the
    weight a candidate *could* have had (every voting model ranking it first at full confidence),
    not the weight the winner happened to get.
    """
    from chemclaw_mcp_rxnpredict.engine.config import Settings
    from chemclaw_mcp_rxnpredict.engine.meta.aggregator import aggregate_forward
    from chemclaw_mcp_rxnpredict.engine.schemas import ForwardPrediction

    settings = Settings()
    settings.model_trust_priors = {"m_a": 1.0, "m_b": 1.0, "m_c": 1.0}

    def pred(model: str, smiles: str, rank: int, score: float) -> ForwardPrediction:
        return ForwardPrediction(product_smiles=smiles, score=score, rank=rank, source_model=model)

    lone_weak = aggregate_forward({"m_a": [pred("m_a", "CCO", 1, 0.0001)]}, settings, 3)
    unanimous = aggregate_forward(
        {name: [pred(name, "CCO", 1, 1.0)] for name in ("m_a", "m_b", "m_c")}, settings, 3
    )
    print(
        f"one model, score 1e-4, rank 1 -> consensus_score={lone_weak[0].consensus_score:.4f} "
        f"(vote_count={lone_weak[0].vote_count})"
    )
    print(
        f"three models, unanimous, score 1.0 -> "
        f"consensus_score={unanimous[0].consensus_score:.4f} "
        f"(vote_count={unanimous[0].vote_count})"
    )
    assert unanimous[0].consensus_score == pytest.approx(1.0)
    assert lone_weak[0].consensus_score < 0.01


async def test_a_disabled_predictor_is_unreachable_through_the_single_model_tool(
    fake_predictors: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One function must decide what this deployment serves, or the control is not a control.

    The ensemble tools narrowed the registry through `_select`; the single-model tools looked the
    predictor up in the raw registry and called it. So a predictor an operator switched off after a
    bad checkpoint bake stayed fully callable through a declared, advertised `read_only` tool — and
    `list_available_models`, the one tool a caller would check, reported it `available: true`.
    """
    monkeypatch.setenv("CHEMCLAW_RXNPREDICT_DISABLED_MODELS", "fake_b,fake_d")
    from chemclaw_mcp_rxnpredict.engine.config import reset_settings_for_tests

    reset_settings_for_tests()
    with pytest.raises(ValueError, match="fake_b"):
        await tools.predict_forward_single_model("fake_b", "CC(=O)Cl.Nc1ccccc1")
    with pytest.raises(ValueError, match="fake_d"):
        await tools.predict_conditions_single_model(
            "fake_d", "CC(=O)Cl.Nc1ccccc1", "CC(=O)Nc1ccccc1"
        )

    listed = tools.list_available_models()
    by_name = {row.name: row for row in listed.forward + listed.conditions}
    for name in ("fake_b", "fake_d"):
        print(f"list_available_models: {name} available={by_name[name].available}")
        assert by_name[name].available is False
        assert by_name[name].unavailable_reason
    assert by_name["fake_a"].available is True


async def test_top_k_is_bounded_on_the_tools_that_are_actually_served() -> None:
    """The bound lived in request envelopes nothing imports; the served schema had none.

    `reaction_t5` passes `top_k` straight into `num_beams` and `num_return_sequences`, so an
    unbounded integer is an unbounded allocation inside a worker thread that a client timeout does
    not stop. `top_k=-1` was accepted too, and `sorted_candidates[:-1]` then dropped the only
    prediction, so `per_model` and `consensus` contradicted each other.
    """
    schemas = {tool.name: tool.inputSchema for tool in await tools.server.list_tools()}
    for name in (
        "predict_forward_reaction",
        "predict_reaction_conditions",
        "predict_forward_single_model",
        "predict_conditions_single_model",
    ):
        top_k = schemas[name]["properties"]["top_k"]
        print(f"{name}: top_k schema = {top_k}")
        assert top_k.get("minimum") == 1, name
        assert top_k.get("maximum") == tools.MAX_TOP_K, name


async def test_a_concurrent_first_request_loads_the_checkpoint_once(
    fake_predictors: None,
) -> None:
    """Three requests arriving before the first load finishes must not be three checkpoints.

    `if not self._loaded: await ...; self._loaded = True` straddles an await with no lock, so every
    coroutine that arrived first saw `False`. For `reaction_t5_v2` each load is a full T5
    checkpoint into a fresh allocation, in a pod sized for one — three at once is an OOMKill and a
    restart back into the same window.
    """
    import asyncio

    from chemclaw_mcp_rxnpredict.engine.predictors.base import BaseForwardPredictor
    from chemclaw_mcp_rxnpredict.engine.schemas import ForwardPrediction

    class CountingPredictor(BaseForwardPredictor):
        def __init__(self) -> None:
            super().__init__()
            self.name = "counting"
            self.description = "counts its own loads"
            self.loads = 0

        def load(self) -> None:
            import time

            time.sleep(0.05)
            self.loads += 1

        def predict_sync(self, reactants: str, top_k: int) -> list[ForwardPrediction]:
            return [
                ForwardPrediction(product_smiles="CCO", score=1.0, rank=1, source_model=self.name)
            ]

    predictor = CountingPredictor()
    await asyncio.gather(*(predictor.predict("C" * (n + 2) + "O", 1) for n in range(3)))
    print(f"load() was called {predictor.loads} time(s) for 3 concurrent first requests")
    assert predictor.loads == 1


# --- A zero-success ensemble ------------------------------------------------------------------
#
# `gather(..., return_exceptions=True)` and `continue` degrade an ensemble one predictor at a time,
# which is right — and it kept degrading all the way to nothing. With every installed predictor
# raising `OSError("egress refused")` — exactly the shape of `EgressForbidden`, which subclasses
# `OSError` — both ensemble tools returned `consensus: []`, `per_model: {}`, `n_models_succeeded: 0`
# and `isError: false`. A vanished checkpoint mount and an egress guard refusing every weight fetch
# both read, from outside the pod, as a healthy server answering a hard question: the tool-call
# counter booked `outcome="ok"`, and the `refused`/`failed` split that exists for precisely this
# showed nothing. A consensus over nothing is not an answer, so it is a refusal.


async def _explode(*_args: object, **_kwargs: object) -> list[object]:
    """The failure shape that motivated this: `EgressForbidden` is an `OSError`."""
    raise OSError("egress refused")


async def test_a_forward_ensemble_with_no_survivors_refuses(
    fake_predictors: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every forward predictor failing is a refusal naming what failed, not an empty consensus."""
    for name in ("fake_a", "fake_b"):
        monkeypatch.setattr(registry.get_forward(name), "predict", _explode)
    with pytest.raises(ValueError) as refusal:
        await tools.predict_forward_reaction("CC(=O)Cl.Nc1ccccc1")
    message = str(refusal.value)
    assert "fake_a" in message and "fake_b" in message
    assert "OSError" in message
    assert "list_available_models" in message


async def test_a_conditions_ensemble_with_no_survivors_refuses(
    fake_predictors: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The condition side has the identical loop and had the identical hole."""
    for name in ("fake_c", "fake_d"):
        monkeypatch.setattr(registry.get_conditions(name), "predict", _explode)
    with pytest.raises(ValueError) as refusal:
        await tools.predict_reaction_conditions("CC(=O)Cl.Nc1ccccc1", "CC(=O)Nc1ccccc1")
    assert "fake_c" in str(refusal.value)


async def test_a_narrowed_ensemble_whose_only_predictor_fails_refuses(
    fake_predictors: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`models=[...]` narrowing to one predictor that then fails is the same zero-success case."""
    monkeypatch.setattr(registry.get_forward("fake_a"), "predict", _explode)
    with pytest.raises(ValueError, match="fake_a"):
        await tools.predict_forward_reaction("CC(=O)Cl.Nc1ccccc1", models=["fake_a"])


async def test_the_refusal_quotes_the_exception_type_and_not_its_message(
    fake_predictors: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal reaches the model verbatim, so it carries the fault's *type*, never its text.

    `connector_app` passes a `ValueError` through unchanged and replaces every other exception, so
    anything folded into this message is published to the caller. A predictor's own exception text
    is where a checkpoint path, a DSN or a token would be; the log line beside it carries the full
    `repr` for an operator, keyed by the same predictor name.
    """

    async def leak(*_args: object, **_kwargs: object) -> list[object]:
        raise RuntimeError("/mnt/secrets/token=hunter2 not found")

    for name in ("fake_a", "fake_b"):
        monkeypatch.setattr(registry.get_forward(name), "predict", leak)
    with pytest.raises(ValueError) as refusal:
        await tools.predict_forward_reaction("CC(=O)Cl.Nc1ccccc1")
    assert "RuntimeError" in str(refusal.value)
    assert "hunter2" not in str(refusal.value)


async def test_a_partial_success_still_answers_and_still_carries_the_spread(
    fake_predictors: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal must not swallow the degraded-but-usable case — an ensemble's whole point."""
    monkeypatch.setattr(registry.get_forward("fake_b"), "predict", _explode)
    result = await tools.predict_forward_reaction("CC(=O)Cl.Nc1ccccc1")
    assert result.n_models_queried == 2
    assert result.n_models_succeeded == 1
    assert set(result.per_model) == {"fake_a"}
    assert result.consensus


async def test_a_single_model_tool_lets_its_predictor_s_failure_through(
    fake_predictors: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single-model tools never had the swallow, and this is what keeps it that way.

    They query one predictor and await it directly, so a fault propagates and `connector_app`
    books `outcome="failed"` and replaces the text. Asserting the absence is what makes the
    ensemble fix above a *narrowing* rather than a claim about the whole server.
    """
    monkeypatch.setattr(registry.get_forward("fake_a"), "predict", _explode)
    with pytest.raises(OSError, match="egress refused"):
        await tools.predict_forward_single_model("fake_a", "CC(=O)Cl.Nc1ccccc1")

    monkeypatch.setattr(registry.get_conditions("fake_c"), "predict", _explode)
    with pytest.raises(OSError, match="egress refused"):
        await tools.predict_conditions_single_model(
            "fake_c", "CC(=O)Cl.Nc1ccccc1", "CC(=O)Nc1ccccc1"
        )
