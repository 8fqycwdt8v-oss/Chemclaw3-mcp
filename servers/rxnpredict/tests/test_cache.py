"""The cache is bounded, canonicalising, and cannot fail a prediction.

Rewritten for the in-process LRU that replaced upstream's diskcache. Three properties matter: it
bounds memory, two spellings of one reaction share a slot, and a key it cannot canonicalise still
produces a key rather than an exception.
"""

from __future__ import annotations

from chemclaw_mcp_rxnpredict.engine.cache import PredictionCache

PAYLOAD = [{"product_smiles": "CCO", "score": 1.0, "rank": 1, "source_model": "m"}]


def test_a_stored_result_comes_back() -> None:
    """The base case; without it nothing else here means anything."""
    cache = PredictionCache(enabled=True, max_entries=8)
    cache.set_forward("m", "CCO", 5, PAYLOAD)
    assert cache.get_forward("m", "CCO", 5) == PAYLOAD


def test_two_spellings_of_one_reaction_share_a_slot() -> None:
    """The key is canonical, so a reordered dot-separated input is a hit and not a second entry."""
    cache = PredictionCache(enabled=True, max_entries=8)
    cache.set_forward("m", "CC(=O)Cl.Nc1ccccc1", 5, PAYLOAD)
    assert cache.get_forward("m", "Nc1ccccc1.CC(=O)Cl", 5) == PAYLOAD


def test_top_k_and_model_are_part_of_the_key() -> None:
    """A top-3 answer is not a top-5 answer, and one model's is not another's."""
    cache = PredictionCache(enabled=True, max_entries=8)
    cache.set_forward("m", "CCO", 5, PAYLOAD)
    assert cache.get_forward("m", "CCO", 3) is None
    assert cache.get_forward("other", "CCO", 5) is None


def test_the_bound_evicts_least_recently_used() -> None:
    """Unbounded, this is a memory leak that grows with conversation length."""
    cache = PredictionCache(enabled=True, max_entries=2)
    cache.set_forward("m", "CCO", 1, PAYLOAD)
    cache.set_forward("m", "CCC", 1, PAYLOAD)
    cache.get_forward("m", "CCO", 1)  # CCO is now the most recently used
    cache.set_forward("m", "CCCC", 1, PAYLOAD)  # evicts CCC, not CCO
    assert cache.get_forward("m", "CCO", 1) == PAYLOAD
    assert cache.get_forward("m", "CCC", 1) is None


def test_an_unparseable_input_still_keys_rather_than_raising() -> None:
    """A cache must never be the thing that fails a prediction."""
    cache = PredictionCache(enabled=True, max_entries=4)
    cache.set_forward("m", "not-a-smiles", 5, PAYLOAD)
    assert cache.get_forward("m", "not-a-smiles", 5) == PAYLOAD


def test_disabled_is_a_no_op_with_the_same_interface() -> None:
    """Call sites must not need a null check, so disabled stores nothing and returns None."""
    cache = PredictionCache(enabled=False, max_entries=8)
    cache.set_forward("m", "CCO", 5, PAYLOAD)
    assert cache.get_forward("m", "CCO", 5) is None


def test_conditions_keys_include_the_product() -> None:
    """The same reactants to two different products are two different questions."""
    cache = PredictionCache(enabled=True, max_entries=8)
    cache.set_conditions("m", "CCO.CC(O)=O", "CCOC(C)=O", 5, PAYLOAD)
    assert cache.get_conditions("m", "CCO.CC(O)=O", "CCOC(C)=O", 5) == PAYLOAD
    assert cache.get_conditions("m", "CCO.CC(O)=O", "CC(=O)OC(C)=O", 5) is None
