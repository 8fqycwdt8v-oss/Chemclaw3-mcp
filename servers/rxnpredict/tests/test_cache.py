"""The cache is bounded, canonicalising, and cannot fail a prediction.

Properties: it bounds memory, two spellings of one reaction share a slot, an input it cannot
canonicalise is declined rather than keyed by the caller's raw text, and declining never raises.
The last two are asserted as separate facts, since "did not raise" alone hides a raw-text key.
"""

from __future__ import annotations

from typing import Any

import chemclaw_mcp_rxnpredict.engine.cache as cache_module
import pytest
from chemclaw_mcp_rxnpredict.engine.cache import COMPONENT, PredictionCache
from chemclaw_mcp_rxnpredict.engine.predictors.base import (
    BaseConditionsPredictor,
    BaseForwardPredictor,
)
from chemclaw_mcp_rxnpredict.engine.schemas import ConditionsPrediction, ForwardPrediction
from mcp_server_kit import degradation
from mcp_server_kit.egress import EgressForbidden
from prometheus_client import REGISTRY

PAYLOAD = [{"product_smiles": "CCO", "score": 1.0, "rank": 1, "source_model": "m"}]


class _StubForward(BaseForwardPredictor):
    """A predictor that always answers, so what is measured is the cache and not the model.

    Real enough to exercise the shipped `predict()`: the base class is what calls the cache twice
    per prediction, which is the wiring these tests exist to hold.
    """

    name = "stub"
    description = "a predictor that answers instantly"

    def load(self) -> None:
        """Nothing to load."""

    def predict_sync(self, reactants: str, top_k: int) -> list[ForwardPrediction]:
        """One prediction, ignoring the arguments."""
        return [ForwardPrediction(product_smiles="CCO", score=1.0, rank=1, source_model=self.name)]


class _StubConditions(BaseConditionsPredictor):
    """The conditions half of `_StubForward`, for the key with two SMILES in it."""

    name = "stub"
    description = "a conditions predictor that answers instantly"

    def load(self) -> None:
        """Nothing to load."""

    def predict_sync(self, reactants: str, product: str, top_k: int) -> list[ConditionsPrediction]:
        """One prediction, ignoring the arguments."""
        return [ConditionsPrediction(score=1.0, rank=1, source_model=self.name)]


def _degraded_total(cause: str | None = None) -> float:
    """This component's degradation count, summed over causes unless one is named.

    Summed by default so `test_a_caller_typo_moves_no_degradation_counter` cannot pass by the
    counter moving on a cause it did not think to name.
    """
    causes = [cause] if cause is not None else sorted(degradation.CAUSES)
    total = 0.0
    for one in causes:
        labels: dict[str, Any] = {
            "server": "rxnpredict",
            "component": COMPONENT,
            "cause": one,
        }
        total += REGISTRY.get_sample_value("chemclaw_mcp_degraded_total", labels) or 0.0
    return total


def test_a_stored_result_comes_back() -> None:
    """The base case; without it nothing else here means anything."""
    cache = PredictionCache(enabled=True, max_entries=8)
    cache.set(cache.key_forward("m", "CCO", 5), PAYLOAD)
    assert cache.get(cache.key_forward("m", "CCO", 5)) == PAYLOAD


def test_two_spellings_of_one_reaction_share_a_slot() -> None:
    """The key is canonical, so a reordered dot-separated input is a hit and not a second entry."""
    cache = PredictionCache(enabled=True, max_entries=8)
    cache.set(cache.key_forward("m", "CC(=O)Cl.Nc1ccccc1", 5), PAYLOAD)
    assert cache.get(cache.key_forward("m", "Nc1ccccc1.CC(=O)Cl", 5)) == PAYLOAD


def test_top_k_and_model_are_part_of_the_key() -> None:
    """A top-3 answer is not a top-5 answer, and one model's is not another's."""
    cache = PredictionCache(enabled=True, max_entries=8)
    cache.set(cache.key_forward("m", "CCO", 5), PAYLOAD)
    assert cache.get(cache.key_forward("m", "CCO", 3)) is None
    assert cache.get(cache.key_forward("other", "CCO", 5)) is None


def test_the_bound_evicts_least_recently_used() -> None:
    """Unbounded, this is a memory leak that grows with conversation length."""
    cache = PredictionCache(enabled=True, max_entries=2)
    cache.set(cache.key_forward("m", "CCO", 1), PAYLOAD)
    cache.set(cache.key_forward("m", "CCC", 1), PAYLOAD)
    cache.get(cache.key_forward("m", "CCO", 1))  # CCO is now the most recently used
    cache.set(cache.key_forward("m", "CCCC", 1), PAYLOAD)  # evicts CCC, not CCO
    assert cache.get(cache.key_forward("m", "CCO", 1)) == PAYLOAD
    assert cache.get(cache.key_forward("m", "CCC", 1)) is None


def test_an_unparseable_input_is_declined_rather_than_keyed_by_its_raw_text() -> None:
    """An unparseable input is declined rather than keyed by its raw text.

    Both halves: the call must not raise (a cache never fails a prediction) and nothing may be
    stored (a string RDKit refused is not an identity).
    """
    cache = PredictionCache(enabled=True, max_entries=4)
    cache.set(cache.key_forward("m", "not-a-smiles", 5), PAYLOAD)
    assert cache.get(cache.key_forward("m", "not-a-smiles", 5)) is None
    assert len(cache._entries) == 0, "an input with no canonical form must mint no entry"


def test_one_molecule_set_spelled_two_ways_never_mints_two_entries() -> None:
    """One molecule set spelled two ways never mints two entries.

    Canonicalisation sorts components and raw text does not, so keying on raw text would store
    `CCO.<garbage>` and `<garbage>.CCO` separately. Declining both makes that impossible.
    """
    cache = PredictionCache(enabled=True, max_entries=8)
    cache.set(cache.key_forward("m", "CCO.Xx9nope", 5), PAYLOAD)
    cache.set(cache.key_forward("m", "Xx9nope.CCO", 5), PAYLOAD)
    assert len(cache._entries) == 0


def test_a_smiles_over_the_structural_limit_is_not_keyed_by_the_string_the_limit_rejects() -> None:
    """A SMILES the structural limit rejects is not keyed by the rejected string.

    The limit refuses before parsing (a large enough molecule overflows the C stack in
    `MolToSmiles`) with the same `ValueError` a typo raises; the cache must not undo that.
    """
    over_limit = "C" * 5000
    cache = PredictionCache(enabled=True, max_entries=4)
    cache.set(cache.key_forward("m", over_limit, 5), PAYLOAD)
    assert len(cache._entries) == 0


def test_a_caller_typo_moves_no_degradation_counter() -> None:
    """A caller's typo moves no degradation counter.

    `chemclaw_mcp_degraded_total` reports a lost component; firing it per bad SMILES would make it
    useless, so the `ValueError` arm is deliberately silent.
    """
    before = _degraded_total()
    cache = PredictionCache(enabled=True, max_entries=4)
    cache.set(cache.key_forward("m", "not-a-smiles", 5), PAYLOAD)
    assert _degraded_total() == before


@pytest.mark.parametrize(
    ("exc", "cause"),
    [
        (EgressForbidden("the guard refused a lookup"), degradation.CAUSE_EGRESS_REFUSED),
        (
            ModuleNotFoundError("No module named 'rdkit'", name="rdkit"),
            degradation.CAUSE_NOT_INSTALLED,
        ),
        (MemoryError(), degradation.CAUSE_RESOURCE_EXHAUSTED),
    ],
)
async def test_a_broken_canonicaliser_is_classified_and_counted(
    monkeypatch: pytest.MonkeyPatch, exc: Exception, cause: str
) -> None:
    """A broken canonicaliser is classified and counted once per degraded answer.

    `EgressForbidden` (an `OSError`), `ImportError` (RDKit absent) and `MemoryError` each must be
    classified rather than read as an ordinary miss. Driven through `predict()` itself, because the
    cache is consulted on the way in and on the way out; the count a scrape reads must be one.
    """
    before = _degraded_total(cause)

    def refuse(_smiles: str) -> str:
        raise exc

    monkeypatch.setattr(cache_module, "canonical_multi_smiles", refuse)
    monkeypatch.setattr(cache_module, "canonical_smiles", refuse)
    cache = PredictionCache(enabled=True, max_entries=4)
    monkeypatch.setattr(cache_module, "_cache", cache)

    predictions = await _StubForward().predict("CCO", 5)

    assert predictions, "the cache must never be the thing that fails a prediction"
    assert len(cache._entries) == 0, "a pod that cannot canonicalise must not guess a key either"
    assert _degraded_total(cause) == before + 1.0


@pytest.mark.parametrize(
    ("exc", "cause"),
    [
        (EgressForbidden("the guard refused a lookup"), degradation.CAUSE_EGRESS_REFUSED),
        (
            ModuleNotFoundError("No module named 'rdkit'", name="rdkit"),
            degradation.CAUSE_NOT_INSTALLED,
        ),
    ],
)
async def test_a_conditions_prediction_counts_one_even_though_it_has_two_smiles(
    monkeypatch: pytest.MonkeyPatch, exc: Exception, cause: str
) -> None:
    """A conditions prediction counts one degradation even though its key has two SMILES.

    The reactant side short-circuits, so a broken RDKit is not counted again for the product side
    or across `get` and `set`; a second failure adds nothing the first did not log.
    """
    before = _degraded_total(cause)

    def refuse(_smiles: str) -> str:
        raise exc

    monkeypatch.setattr(cache_module, "canonical_multi_smiles", refuse)
    monkeypatch.setattr(cache_module, "canonical_smiles", refuse)
    cache = PredictionCache(enabled=True, max_entries=4)
    monkeypatch.setattr(cache_module, "_cache", cache)

    predictions = await _StubConditions().predict("CCO", "CCOC", 5)

    assert predictions, "the cache must never be the thing that fails a prediction"
    assert len(cache._entries) == 0
    assert _degraded_total(cause) == before + 1.0


def test_disabled_is_a_no_op_with_the_same_interface() -> None:
    """Call sites must not need a null check, so disabled stores nothing and returns None."""
    cache = PredictionCache(enabled=False, max_entries=8)
    cache.set(cache.key_forward("m", "CCO", 5), PAYLOAD)
    assert cache.get(cache.key_forward("m", "CCO", 5)) is None


def test_conditions_keys_include_the_product() -> None:
    """The same reactants to two different products are two different questions."""
    cache = PredictionCache(enabled=True, max_entries=8)
    cache.set(cache.key_conditions("m", "CCO.CC(O)=O", "CCOC(C)=O", 5), PAYLOAD)
    assert cache.get(cache.key_conditions("m", "CCO.CC(O)=O", "CCOC(C)=O", 5)) == PAYLOAD
    assert cache.get(cache.key_conditions("m", "CCO.CC(O)=O", "CC(=O)OC(C)=O", 5)) is None
