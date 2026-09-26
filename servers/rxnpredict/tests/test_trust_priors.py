"""Tests for the trust-prior store and per-class lookup."""

from __future__ import annotations

from pathlib import Path

import pytest
from chemclaw_mcp_rxnpredict.engine.config import DEFAULT_MODEL_TRUST_PRIORS, Settings
from chemclaw_mcp_rxnpredict.engine.meta.trust_priors import (
    effective_prior,
    load_priors_file,
    save_priors_file,
)


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_priors_file(tmp_path / "missing.json") == {}


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "priors.json"
    data = {
        "amide_formation": {"reaction_t5_v2": 0.91, "molecular_transformer": 0.83},
        "suzuki_coupling": {"reaction_t5_v2": 0.78},
    }
    save_priors_file(path, data)
    out = load_priors_file(path)
    assert out == data


def test_load_malformed_file_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("not json")
    assert load_priors_file(path) == {}


def test_effective_prior_prefers_class_specific() -> None:
    global_priors = {"m": 0.5}
    class_priors = {"suzuki_coupling": {"m": 0.95}}
    p = effective_prior("m", "suzuki_coupling", global_priors, class_priors)
    assert p == 0.95


def test_effective_prior_falls_back_to_global_when_class_missing() -> None:
    global_priors = {"m": 0.5}
    class_priors = {"amide_formation": {"m": 0.95}}
    p = effective_prior("m", "suzuki_coupling", global_priors, class_priors)
    assert p == 0.5


def test_effective_prior_falls_back_to_global_when_no_class() -> None:
    global_priors = {"m": 0.5}
    p = effective_prior("m", None, global_priors, {})
    assert p == 0.5


def test_effective_prior_default_for_unknown_model() -> None:
    p = effective_prior("unseen_model", None, {}, {}, default=0.42)
    assert p == 0.42


def test_class_other_skips_class_lookup() -> None:
    from chemclaw_mcp_rxnpredict.engine.meta.classifier import CLASS_OTHER

    global_priors = {"m": 0.5}
    # Even if someone puts a value under "other", we want global priors
    class_priors = {CLASS_OTHER: {"m": 0.99}}
    p = effective_prior("m", CLASS_OTHER, global_priors, class_priors)
    assert p == 0.5


_PRIORS_ENV = "CHEMCLAW_RXNPREDICT_MODEL_TRUST_PRIORS"


def test_an_env_prior_adjusts_the_table_rather_than_replacing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The measured defect: one named weight used to leave every other predictor unweighted.

    Driven through the real environment source, because the replacement happened there — a test
    constructing `Settings(model_trust_priors=...)` would not show which path pydantic-settings
    takes to the validator.
    """
    monkeypatch.setenv(_PRIORS_ENV, '{"parrot": 9.9}')
    priors = Settings().model_trust_priors
    assert priors["parrot"] == 9.9
    assert set(priors) == set(DEFAULT_MODEL_TRUST_PRIORS)
    unchanged = {name: w for name, w in priors.items() if name != "parrot"}
    assert unchanged == {n: w for n, w in DEFAULT_MODEL_TRUST_PRIORS.items() if n != "parrot"}
    # And what that means where it is read: the aggregator's weight for a predictor the operator
    # did not name is still its default, not `effective_prior`'s unweighted 0.5.
    assert effective_prior("reaction_t5_v2", None, priors, {}) == 1.00


def test_no_env_value_is_the_default_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped state, and a fresh copy each time so one Settings cannot edit another's."""
    monkeypatch.delenv(_PRIORS_ENV, raising=False)
    first, second = Settings().model_trust_priors, Settings().model_trust_priors
    assert first == dict(DEFAULT_MODEL_TRUST_PRIORS)
    first["parrot"] = 0.1
    assert second["parrot"] == DEFAULT_MODEL_TRUST_PRIORS["parrot"]


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ('{"parot": 0.9}', "does not weight"),
        ('{"parrot": 0}', "finite and above zero"),
        ('{"parrot": -1.0}', "finite and above zero"),
        ('{"parrot": "high"}', "must be a number"),
        ('{"parrot": true}', "must be a number"),
        ("[0.9]", "JSON object"),
    ],
)
def test_an_env_prior_the_aggregator_cannot_mean_is_refused(
    monkeypatch: pytest.MonkeyPatch, raw: str, reason: str
) -> None:
    """A typo'd predictor id would set nothing and read as done; a non-positive weight inverts or
    silences a vote. Both are refused at settings load, naming what was wrong."""
    monkeypatch.setenv(_PRIORS_ENV, raw)
    with pytest.raises(ValueError, match=reason):
        Settings()
