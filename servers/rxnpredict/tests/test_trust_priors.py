"""Tests for the trust-prior store and per-class lookup."""

from __future__ import annotations

from pathlib import Path

from chemclaw_mcp_rxnpredict.engine.meta.trust_priors import (
    effective_prior,
    load_priors_file,
    save_priors_file,
)


def test_load_missing_file_returns_empty(tmp_path: Path):
    assert load_priors_file(tmp_path / "missing.json") == {}


def test_round_trip(tmp_path: Path):
    path = tmp_path / "priors.json"
    data = {
        "amide_formation": {"reaction_t5_v2": 0.91, "molecular_transformer": 0.83},
        "suzuki_coupling": {"reaction_t5_v2": 0.78},
    }
    save_priors_file(path, data)
    out = load_priors_file(path)
    assert out == data


def test_load_malformed_file_returns_empty(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text("not json")
    assert load_priors_file(path) == {}


def test_effective_prior_prefers_class_specific():
    global_priors = {"m": 0.5}
    class_priors = {"suzuki_coupling": {"m": 0.95}}
    p = effective_prior("m", "suzuki_coupling", global_priors, class_priors)
    assert p == 0.95


def test_effective_prior_falls_back_to_global_when_class_missing():
    global_priors = {"m": 0.5}
    class_priors = {"amide_formation": {"m": 0.95}}
    p = effective_prior("m", "suzuki_coupling", global_priors, class_priors)
    assert p == 0.5


def test_effective_prior_falls_back_to_global_when_no_class():
    global_priors = {"m": 0.5}
    p = effective_prior("m", None, global_priors, {})
    assert p == 0.5


def test_effective_prior_default_for_unknown_model():
    p = effective_prior("unseen_model", None, {}, {}, default=0.42)
    assert p == 0.42


def test_class_other_skips_class_lookup():
    from chemclaw_mcp_rxnpredict.engine.meta.classifier import CLASS_OTHER

    global_priors = {"m": 0.5}
    # Even if someone puts a value under "other", we want global priors
    class_priors = {CLASS_OTHER: {"m": 0.99}}
    p = effective_prior("m", CLASS_OTHER, global_priors, class_priors)
    assert p == 0.5
