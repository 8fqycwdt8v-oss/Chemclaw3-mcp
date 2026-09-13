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

# One guarded `mark_unavailable` call per predictor module: six forward, five conditions. A number
# rather than an emptiness check, because an emptiness check over a collected list is satisfied by
# the calls being gone — see `test_every_predictor_module_hands_its_exception_to_the_registry`.
PREDICTOR_GUARDS = 11


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
    """A module that raised outside its own guard, where nothing of its kind is left to serve.

    This checkout registers no conditions predictor, so the broken entry below takes the last one —
    which is the threshold now: a *kind* this pod cannot serve at all, rather than any broken
    predictor. `test_a_broken_predictor_beside_a_working_one_is_ready` is the counterfactual, and it
    is the case this arm used to get wrong.
    """
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
    `except`. The *cause* — and therefore what this pod reports about itself — is derived from the
    exception object, and a module that passes only its reason string classifies as `not_installed`
    whatever actually happened. Those `except` blocks run at import, in a checkout where the imports
    they guard all fail the same way, so no test can drive one of them into a *different* failure.
    AST rather than grep, for the reason `mcp_server_kit/no_egress.py` gives: spellings differ as
    text and agree as a tree.

    **This test was vacuous in two ways and this docstring claimed otherwise**, which is worse than
    the test being absent. It asserted `not missing` over a list of calls lacking an `exc` keyword:

    - `exc=exc` -> `exc=None` in one module left **295 passed**, and `None` takes
      `mark_unavailable`'s documented `CAUSE_NOT_INSTALLED` default, so a corrupt checkpoint read as
      an extra nobody installed;
    - deleting the `mark_unavailable(...)` call outright left **295 passed**, because `assert not
      missing` is satisfied by *zero* matching call sites — a predictor dropping out of
      `list_available_models`, out of the degraded counter and out of readiness, silently.

    So the count is asserted, and the keyword's **value** is checked against the name the enclosing
    `except ... as <name>` binds — which is the thing that actually has to be true, and which
    `exc=None` does not satisfy.
    """
    import ast
    from pathlib import Path

    root = Path(registry.__file__).parent
    wrong: list[str] = []
    sites: list[str] = []
    for module in sorted(root.glob("*/*.py")):
        if module.name == "__init__.py":
            continue
        for handler in ast.walk(ast.parse(module.read_text())):
            if not isinstance(handler, ast.ExceptHandler):
                continue
            for node in ast.walk(handler):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "mark_unavailable"
                ):
                    continue
                where = f"{module.parent.name}/{module.name}:{node.lineno}"
                sites.append(where)
                passed = next((kw.value for kw in node.keywords if kw.arg == "exc"), None)
                if not (isinstance(passed, ast.Name) and passed.id == handler.name):
                    shown = ast.unparse(passed) if passed else "<none>"
                    wrong.append(f"{where} passes exc={shown}")

    assert not wrong, (
        "a predictor module must hand `mark_unavailable` the exception its own `except` bound, or "
        f"the cause is guessed from the reason text instead of classified: {wrong}"
    )
    assert len(sites) == PREDICTOR_GUARDS, (
        f"found {len(sites)} guarded `mark_unavailable` call site(s) {sites}, expected "
        f"{PREDICTOR_GUARDS}: one per predictor module. A site that disappeared is a predictor "
        "that drops out of the degraded counter, out of list_available_models and out of this "
        "probe without anything going red — which an emptiness check allowed"
    )


def test_the_module_map_agrees_with_the_registry_names() -> None:
    """`_FORWARD_MODULES`/`_CONDITIONS_MODULES` name each predictor, and must name it correctly.

    The map exists because `discover_predictors`'s catch-all needs a registry name for a module it
    could not import, and because `degradation.record` clamps its `component` label to a declared
    set. Both make the map a second statement of names the predictor classes already carry, so the
    two are compared here rather than trusted: on a checkout with no extras every predictor records
    itself unavailable *under its class's own `name`*, so the union of the registered and the
    unavailable names is exactly what those classes say.
    """
    declared = set(registry._FORWARD_MODULES.values()) | set(registry._CONDITIONS_MODULES.values())
    registry.discover_predictors()
    observed = (
        {predictor.name for predictor in registry.list_forward()}
        | {predictor.name for predictor in registry.list_conditions()}
        | set(registry.unavailable())
    )
    assert declared == observed, (
        f"the module map declares {sorted(declared - observed)} that no predictor answers to, "
        f"and misses {sorted(observed - declared)}. The map is what the catch-all files a broken "
        "module under and what bounds this server's metric labels, so a drift here is a predictor "
        "counted under a name list_available_models does not use"
    )


def test_a_broken_predictor_beside_a_working_one_is_ready(
    clean_registry: None, fake_predictors: None
) -> None:
    """A pod serving ten of eleven predictors is serving, and taking it out is the larger harm.

    This is the arm that was wrong. `verify_predictors` refused for *any* entry whose cause was
    permanent, and a missing checkpoint file raises `FileNotFoundError`, which is an `OSError`,
    which
    classifies `failed`, which is permanent. Driven on the real app: a pod answering with ten of its
    eleven optional predictors — the normal case by design — answered 503 for one broken one, and
    since a restart cannot recreate a missing file the result was `CrashLoopBackOff` on a capability
    that had been working.

    The counterfactual is the test below: with *no* forward predictor left, the same broken entry is
    a refusal, because then every forward call fails rather than answering with less.
    """
    registry._UNAVAILABLE.clear()
    registry.mark_unavailable(
        "megan",
        "forward",
        "checkpoint missing",
        exc=FileNotFoundError(2, "No such file", "/mnt/models/megan/model.ckpt"),
    )
    assert registry.unavailable()["megan"].cause in degradation.PERMANENT_CAUSES, "the premise"
    assert registry.list_forward(), "the premise: something of this kind is still registered"
    verify_predictors()


def test_a_kind_with_nothing_left_and_something_broken_is_unready(clean_registry: None) -> None:
    """The measured defect the probe exists for: a pod in service that cannot serve a whole tool.

    With `CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS=reaction_t5_v2` against a build without it,
    `/healthz` was 200 and `predict_forward_reaction` raised "no forward predictors are available in
    this deployment" on the first call. The distinction from a developer checkout — which also has
    none — is the *cause*: absent is a decision, broken is an image.
    """
    registry._UNAVAILABLE.clear()
    registry.mark_unavailable(
        "megan", "forward", "checkpoint missing", exc=RuntimeError("truncated checkpoint")
    )
    assert not registry.list_forward(), "the premise: this checkout registers no forward predictor"
    with pytest.raises(RuntimeError) as unready:
        verify_predictors()
    assert "no forward predictor at all" in str(unready.value)


def test_the_catch_all_files_a_broken_module_under_its_registry_name(
    clean_registry: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`discover_predictors`'s catch-all, driven — the one path the module map exists for.

    Ten of eleven modules have a short name equal to their predictor's registry name, so the
    disagreement is visible on exactly one: `forward/reaction_t5` registers `reaction_t5_v2`. The
    catch-all used the short name, so a module that blew up *outside* its own guard landed in
    `_UNAVAILABLE`, in the degraded counter and in this probe under a key that
    `list_available_models` and `CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS` do not use — so an
    operator grepping `/metrics` for the advertised id found nothing.

    `test_the_module_map_agrees_with_the_registry_names` cannot see this: in a checkout where every
    module imports (the guards are *inside* them) the catch-all never fires, so the defect was green
    there too. Driven: reverting the name to `modname.rsplit(".", 1)[-1]` left the whole
    `rxnpredict` suite passing until this test existed.
    """
    import importlib

    broken = "chemclaw_mcp_rxnpredict.engine.predictors.forward.reaction_t5"
    real = importlib.import_module

    def refuse(name: str, *args: object, **kwargs: object) -> object:
        """Fail the way a module with a syntax error or a bad top-level import fails."""
        if name == broken:
            raise RuntimeError("a top-level import this module does not guard")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(registry.importlib, "import_module", refuse)
    monkeypatch.setattr(registry, "_DISCOVERY_DONE", False)
    registry._UNAVAILABLE.clear()
    registry.discover_predictors()

    assert "reaction_t5_v2" in registry.unavailable(), (
        "the catch-all must file a broken module under the registry name the rest of this server "
        "addresses it by"
    )
    assert "reaction_t5" not in registry.unavailable(), (
        "and not under the module's short name, which is a second component label for one predictor"
    )
    assert registry.unavailable()["reaction_t5_v2"].cause == degradation.CAUSE_FAILED
