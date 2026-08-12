"""The vendored trust priors validate against their own manifest, and against the models they name.

The priors are the numbers that decide every ranking this server produces. Two failure modes are
worth a test each: the file not being the one that was reviewed (the checksum), and the file naming
a predictor that does not exist — a typo in a model name would silently give that class no
weighting at all rather than erroring.
"""

from __future__ import annotations

import json

from chemclaw_mcp_rxnpredict.engine.config import DATA_DIR, Settings
from chemclaw_mcp_rxnpredict.engine.meta.classifier import ALL_CLASSES
from chemclaw_mcp_rxnpredict.engine.meta.trust_priors import load_vendored_priors
from mcp_server_kit import load_dataset


def test_the_priors_match_their_checksum() -> None:
    """A swapped or truncated priors file fails here, at startup, not in a ranking."""
    dataset = load_dataset(DATA_DIR, records_file="trust_priors.json")
    assert dataset.name == "rxnpredict-trust-priors"
    assert dataset.licence
    assert dataset.retrieved_from


def test_the_shipped_table_is_empty_and_says_why() -> None:
    """Shipped empty on purpose: inventing per-class weights would be fabricating the rankings.

    If this ever fails, a calibration has been run — which is welcome, and means the manifest's
    description and version need updating with it.
    """
    dataset = load_dataset(DATA_DIR, records_file="trust_priors.json")
    assert json.loads(dataset.records_path.read_text(encoding="utf-8")) == {}
    assert "SHIPPED EMPTY ON PURPOSE" in dataset.description


def test_every_named_class_and_model_is_one_the_code_knows() -> None:
    """A typo in a class or model name costs that weighting silently, so it is checked."""
    priors = load_vendored_priors(DATA_DIR)
    known_models = set(Settings().model_trust_priors)
    for reaction_class, weights in priors.items():
        assert reaction_class in ALL_CLASSES, f"unknown reaction class {reaction_class!r}"
        for model in weights:
            assert model in known_models, f"unknown predictor {model!r} in {reaction_class!r}"


def test_the_global_priors_cover_every_predictor_this_build_knows() -> None:
    """A predictor with no prior falls back to a bare default, which is a silent demotion."""
    from chemclaw_mcp_rxnpredict.engine.predictors import unavailable

    priors = set(Settings().model_trust_priors)
    for name in unavailable():
        assert name in priors, f"{name} has no trust prior, so its votes would weigh the default"
