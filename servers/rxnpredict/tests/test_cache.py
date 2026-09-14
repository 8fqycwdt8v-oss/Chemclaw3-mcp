"""The cache is bounded, canonicalising, and cannot fail a prediction.

Rewritten for the in-process LRU that replaced upstream's diskcache. Four properties matter: it
bounds memory, two spellings of one reaction share a slot, an input it cannot canonicalise is
declined rather than keyed by the caller's own text, and declining never raises.

The last two are one decision and are tested as three separate facts, because the defect they
replaced passed a test that asserted only the third
(`D-2026-09-13-a-cache-key-derived-from-text-nobody-validated-is-not-a-key`).
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
    """The defect this replaced: unvalidated caller text became the cache's idea of an identity.

    Both halves are asserted, because only the first of them is the fix. The call must not raise —
    a cache is never the thing that fails a prediction — *and* nothing may be stored, because a key
    derived from a string RDKit refused is not a key. Asserting only "it did not raise" is what let
    the raw-text fallback ship.
    """
    cache = PredictionCache(enabled=True, max_entries=4)
    cache.set(cache.key_forward("m", "not-a-smiles", 5), PAYLOAD)
    assert cache.get(cache.key_forward("m", "not-a-smiles", 5)) is None
    assert len(cache._entries) == 0, "an input with no canonical form must mint no entry"


def test_one_molecule_set_spelled_two_ways_never_mints_two_entries() -> None:
    """The measured defect, in the case that produced it.

    `canonical_multi_smiles` sorts its components and raw text does not, so under the raw-text
    fallback `CCO.<garbage>` and `<garbage>.CCO` — one set of molecules, two spellings — were two
    rows. Declining both is what makes that impossible rather than merely unlikely.
    """
    cache = PredictionCache(enabled=True, max_entries=8)
    cache.set(cache.key_forward("m", "CCO.Xx9nope", 5), PAYLOAD)
    cache.set(cache.key_forward("m", "Xx9nope.CCO", 5), PAYLOAD)
    assert len(cache._entries) == 0


def test_a_smiles_over_the_structural_limit_is_not_keyed_by_the_string_the_limit_rejects() -> None:
    """`mcp_server_kit.limits` refuses before parsing; the cache must not undo that by keying it.

    The bound exists because `MolToSmiles` on a large enough molecule overflows the C stack and
    kills the pod, so its refusal arrives as the same `ValueError` an ordinary typo does. Under the
    old fallback the 5,000-character string the bound rejected became the cache key.
    """
    over_limit = "C" * 5000
    cache = PredictionCache(enabled=True, max_entries=4)
    cache.set(cache.key_forward("m", over_limit, 5), PAYLOAD)
    assert len(cache._entries) == 0


def test_a_caller_typo_moves_no_degradation_counter() -> None:
    """A bad argument is a fact about the caller, not a component of this pod going missing.

    `chemclaw_mcp_degraded_total` is what a scrape reads to say a predictor or a dataset has been
    lost. Firing it on every unparseable SMILES would make that series useless for the thing it
    exists for, so the `ValueError` arm is deliberately silent.
    """
    before = _degraded_total()
    cache = PredictionCache(enabled=True, max_entries=4)
    cache.set(cache.key_forward("m", "not-a-smiles", 5), PAYLOAD)
    assert _degraded_total() == before


@pytest.mark.parametrize(
    ("exc", "cause"),
    [
        (EgressForbidden("the guard refused a lookup"), degradation.CAUSE_EGRESS_REFUSED),
        (ImportError("no module named rdkit"), degradation.CAUSE_NOT_INSTALLED),
        (MemoryError(), degradation.CAUSE_RESOURCE_EXHAUSTED),
    ],
)
async def test_a_broken_canonicaliser_is_classified_and_counted(
    monkeypatch: pytest.MonkeyPatch, exc: Exception, cause: str
) -> None:
    """One degraded answer is one increment — and this test shipped seeing half the path.

    All three of these were measured returning the caller's raw text and moving no counter:
    `EgressForbidden` is an `OSError` and so reads as nothing in particular, `ImportError` is RDKit
    absent from the image, `MemoryError` is the pod at its ceiling. That half is unchanged.

    **What changed is what drives it.** This asserted `+1.0` after calling the cache's own
    `set_forward` alone, while its docstring claimed to cover "the wiring as well as the branch" —
    the wiring makes *two* calls, `get_*` on the way in and `set_*` on the way out, each of which
    derived the key and so each of which counted. Driven on the real
    `BaseForwardPredictor.predict`, one degraded answer moved `chemclaw_mcp_degraded_total` by
    **2.0**; on `BaseConditionsPredictor.predict`, where both sides were canonicalised before either
    was tested, by **4.0**. So this drives `predict()` itself, and asserts the count a scrape is
    meant to read: one.
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
        (ImportError("no module named rdkit"), degradation.CAUSE_NOT_INSTALLED),
    ],
)
async def test_a_conditions_prediction_counts_one_even_though_it_has_two_smiles(
    monkeypatch: pytest.MonkeyPatch, exc: Exception, cause: str
) -> None:
    """Two SMILES, one answer, one increment — this was the 4x arm.

    A conditions key names a reactant side and a product side, and both were canonicalised before
    either was tested. One broken RDKit therefore fired twice per derivation and, over `get` and
    `set`, four times per prediction. There is nothing to learn from the second failure that the
    first has not already logged and classified, so the reactants short-circuit.
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
