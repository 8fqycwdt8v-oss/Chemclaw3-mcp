"""`register_requested` — the operator-facing half of the deterministic doubles.

These tests exist because the feature they cover was documented and inert. `base_doubles.py` said
an operator could set `CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS=fake_a` and get a working tool
surface with no model weights, and `Chemclaw3`'s four-repo e2e harness set exactly that env var —
but nothing in the package ever constructed a double. Only `tests/conftest.py` did, so every unit
test passed while a real `uvicorn` came up with an empty registry.

The assertion that matters most here is the negative one: the default configuration, and `"*"`,
must register nothing. A fake predictor that can reach a production ensemble by accident is the
failure this module's header forbids, and it would be a far worse bug than the one being fixed.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from chemclaw_mcp_rxnpredict.engine import predictors as registry
from chemclaw_mcp_rxnpredict.engine.base_doubles import (
    FakeForwardPredictor,
    register_requested,
)
from chemclaw_mcp_rxnpredict.engine.config import get_settings, reset_settings_for_tests


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """An empty registry and fresh settings per test, restored afterwards."""
    saved_forward = dict(registry._FORWARD)
    saved_conditions = dict(registry._CONDITIONS)
    registry._FORWARD.clear()
    registry._CONDITIONS.clear()
    reset_settings_for_tests()
    yield
    registry._FORWARD.clear()
    registry._FORWARD.update(saved_forward)
    registry._CONDITIONS.clear()
    registry._CONDITIONS.update(saved_conditions)
    reset_settings_for_tests()


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> object:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    reset_settings_for_tests()
    return get_settings()


def test_named_doubles_are_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact names asked for appear in the registry — the bug this function fixes."""
    settings = _settings(
        monkeypatch,
        CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS="fake_a",
        CHEMCLAW_RXNPREDICT_ENABLED_CONDITIONS_MODELS="fake_c",
    )

    registered = register_requested(settings)  # type: ignore[arg-type]

    assert registered == ["fake_a", "fake_c"]
    assert [p.name for p in registry.list_forward()] == ["fake_a"]
    assert [p.name for p in registry.list_conditions()] == ["fake_c"]


def test_default_configuration_registers_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped default must not grow a fake predictor. This is the safety property."""
    settings = _settings(monkeypatch)

    assert register_requested(settings) == []  # type: ignore[arg-type]
    assert registry.list_forward() == []
    assert registry.list_conditions() == []


def test_star_registers_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """`"*"` means "every real predictor", never "every double" — the accident to prevent."""
    settings = _settings(
        monkeypatch,
        CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS="*",
        CHEMCLAW_RXNPREDICT_ENABLED_CONDITIONS_MODELS="*",
    )

    assert register_requested(settings) == []  # type: ignore[arg-type]
    assert registry.list_forward() == []


def test_unknown_names_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """The enabled list names real predictors too: a non-double is a filter entry, not a fault."""
    settings = _settings(
        monkeypatch,
        CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS="reaction_t5_v2,fake_b",
    )

    assert register_requested(settings) == ["fake_b"]  # type: ignore[arg-type]


def test_a_real_predictor_of_the_same_name_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registration runs after discovery, so an already-present name is left alone, not duplicated.

    `register_forward` raises on a duplicate, so without the skip this call would crash the server
    at import rather than quietly preferring the real predictor.
    """
    incumbent = FakeForwardPredictor("fake_a", ["CCO"])
    registry.register_forward(incumbent)
    settings = _settings(monkeypatch, CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS="fake_a")

    assert register_requested(settings) == []  # type: ignore[arg-type]
    assert registry.get_forward("fake_a") is incumbent


def test_registering_twice_is_harmless(monkeypatch: pytest.MonkeyPatch) -> None:
    """Idempotent, so an import that runs twice under a reloader does not raise on the duplicate."""
    settings = _settings(monkeypatch, CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS="fake_a")

    assert register_requested(settings) == ["fake_a"]  # type: ignore[arg-type]
    assert register_requested(settings) == []  # type: ignore[arg-type]
    assert len(registry.list_forward()) == 1
