"""This server's answer is an ensemble, and `/healthz` did not look at it.

Every predictor is optional, imported at startup, and a module that raises is recorded in the
registry's unavailable map and logged. That is the right handling and it was the whole handling:
measured before this file, with `CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS=reaction_t5_v2` against
a build that does not carry it, `/healthz` answered **200** and `predict_forward_reaction` raised
"no forward predictors are available in this deployment" on the first call — a pod in service that
could not serve.

The hard half is the *other* direction, and it is why this is not simply "unready when the registry
is thin": a developer's checkout carries none of the ML extras and reports eleven predictors
unavailable, which is the design working. So the tests below drive both arms, and the one that
keeps the fleet honest is the first.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from chemclaw_mcp_rxnpredict.engine import predictors as registry
from chemclaw_mcp_rxnpredict.engine.config import reset_settings_for_tests
from chemclaw_mcp_rxnpredict.engine.readiness import verify_predictors
from mcp_server_kit import degradation
from prometheus_client import REGISTRY


@pytest.fixture
def clean_registry() -> Iterator[None]:
    """Restore the process-wide registry, since it is filled once at import and shared."""
    saved = dict(registry._UNAVAILABLE)
    reset_settings_for_tests()
    try:
        yield
    finally:
        registry._UNAVAILABLE.clear()
        registry._UNAVAILABLE.update(saved)
        reset_settings_for_tests()


def test_a_checkout_with_no_extras_installed_is_ready(clean_registry: None) -> None:
    """The counter-example that shapes the rule: absent is a decision, not a fault."""
    registry._UNAVAILABLE.clear()
    registry.mark_unavailable(
        "reaction_t5_v2", "forward", "missing optional deps", exc=ModuleNotFoundError("torch")
    )
    verify_predictors()


def test_a_predictor_this_image_carries_and_broke_is_unready(clean_registry: None) -> None:
    """A module that raised outside its own optional-dependency guard is a broken image."""
    registry._UNAVAILABLE.clear()
    registry.mark_unavailable(
        "rxn_insight", "conditions", "import failed", exc=RuntimeError("corrupt SMIRKS table")
    )
    with pytest.raises(RuntimeError) as unready:
        verify_predictors()
    assert "rxn_insight" in str(unready.value)


def test_an_egress_refusal_during_load_is_unready(clean_registry: None) -> None:
    """It is an `OSError`, so nothing downstream separates it from a transient network fault."""
    from mcp_server_kit.egress import EgressForbidden

    registry._UNAVAILABLE.clear()
    registry.mark_unavailable(
        "reaction_t5_v2", "forward", "import failed", exc=EgressForbidden("huggingface.co")
    )
    with pytest.raises(RuntimeError) as unready:
        verify_predictors()
    assert degradation.CAUSE_EGRESS_REFUSED in str(unready.value)


def test_an_enabled_model_this_build_does_not_have_is_unready(
    clean_registry: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loudest case: somebody wrote the name down and the server will answer with nothing."""
    registry._UNAVAILABLE.clear()
    monkeypatch.setenv("CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS", "reaction_t5_v2")
    reset_settings_for_tests()
    with pytest.raises(RuntimeError) as unready:
        verify_predictors()
    assert "forward/reaction_t5_v2" in str(unready.value)


def test_an_enabled_model_that_is_registered_is_ready(
    clean_registry: None, fake_predictors: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction, so the test above is about the name and not about the variable."""
    registry._UNAVAILABLE.clear()
    monkeypatch.setenv("CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS", "fake_a")
    reset_settings_for_tests()
    verify_predictors()


def test_losing_a_predictor_moves_a_series_an_operator_scrapes(clean_registry: None) -> None:
    """A smaller ensemble was visible only in a log line and in a tool nobody has to call."""
    labels = {
        "server": "rxnpredict",
        "component": "megan",
        "cause": degradation.CAUSE_FAILED,
    }
    before = REGISTRY.get_sample_value("chemclaw_mcp_degraded_total", labels) or 0.0
    registry.mark_unavailable("megan", "forward", "import failed", exc=RuntimeError("bad weights"))
    assert REGISTRY.get_sample_value("chemclaw_mcp_degraded_total", labels) == before + 1.0
    assert registry.unavailable()["megan"].cause == degradation.CAUSE_FAILED


def test_every_predictor_module_hands_its_exception_to_the_registry() -> None:
    """The eleven call sites, read as source, because behaviour cannot reach them here.

    Each predictor module guards its own optional import and calls `mark_unavailable` from the
    `except`. The *cause* — and therefore whether this pod stays in service — is derived from the
    exception object, and a module that passes only its reason string classifies as `not_installed`
    whatever actually happened. Measured: dropping `exc=exc` from one module left this server's
    whole suite green, so a broken checkpoint in that predictor would have read as an extra nobody
    installed and the probe would have passed it.

    Those `except` blocks run at import, in a checkout where the imports they guard all fail the
    same way, so no test can drive one of them into a *different* failure. AST rather than grep, for
    the reason `mcp_server_kit/no_egress.py` gives: spellings differ as text and agree as a tree.
    """
    import ast
    from pathlib import Path

    root = Path(registry.__file__).parent
    missing = []
    for module in sorted(root.glob("*/*.py")):
        if module.name == "__init__.py":
            continue
        for node in ast.walk(ast.parse(module.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "mark_unavailable"
                and not any(keyword.arg == "exc" for keyword in node.keywords)
            ):
                missing.append(f"{module.parent.name}/{module.name}:{node.lineno}")
    assert not missing, f"mark_unavailable called without `exc=`: {missing}"
