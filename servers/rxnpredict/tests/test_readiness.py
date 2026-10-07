"""`/healthz` looks at the ensemble this server answers with.

Every predictor is optional, so a thin registry is normal (a developer checkout has none). A pod
is unready only when a whole kind of prediction is gone and something broke to take it, as when
an enabled model fails to load and every call would raise. Both arms are driven.
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
    """A broken predictor that leaves its kind with nothing to serve makes the pod unready.

    This checkout registers no conditions predictor, so the broken entry is the last one. The
    counterfactual is `test_a_broken_predictor_beside_a_working_one_is_ready`.
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
    """Every predictor module hands its exception object to the registry, read as source.

    The reported cause is derived from the exception, so a guard passing only a reason string (or
    `exc=None`) classifies as `not_installed` whatever happened. The guards run at import, where
    every optional import fails the same way, so no test can drive them; the AST is read instead.
    The count of call sites is asserted (a missing call is silent), and each `exc=` value must be
    the name its enclosing `except ... as <name>` binds.
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


def test_an_installed_extra_that_will_not_load_is_broken_rather_than_absent(
    clean_registry: None,
) -> None:
    """An installed extra that will not load is broken rather than absent.

    Each guard declares which modules it imports, and only a `ModuleNotFoundError` naming one of
    those is absent; a torch whose CUDA library fails, or a missing transitive dependency, is a
    broken image that `verify_predictors` must be able to refuse.
    """
    registry._UNAVAILABLE.clear()
    registry.mark_unavailable(
        "parrot",
        "conditions",
        "missing optional deps",
        exc=ImportError("libcudart.so.11.0: cannot open shared object file"),
        optional=("torch",),
    )
    registry.mark_unavailable(
        "reaction_t5_v2",
        "forward",
        "missing optional deps",
        exc=ModuleNotFoundError("No module named 'tokenizers'", name="tokenizers"),
        optional=("transformers",),
    )
    registry.mark_unavailable(
        "megan",
        "forward",
        "missing optional deps",
        exc=ModuleNotFoundError("No module named 'dgl'", name="dgl"),
        optional=("dgl", "torch"),
    )
    causes = {name: entry.cause for name, entry in registry.unavailable().items()}
    assert causes == {
        "parrot": degradation.CAUSE_FAILED,
        "reaction_t5_v2": degradation.CAUSE_FAILED,
        "megan": degradation.CAUSE_NOT_INSTALLED,
    }
    with pytest.raises(RuntimeError) as unready:
        verify_predictors()
    assert "reaction_t5_v2 (failed)" in str(unready.value), (
        "the broken predictor was the last of its kind, and the refusal must name it"
    )


def test_every_guard_declares_the_modules_it_imports() -> None:
    """Each guard's `optional=` declaration matches its own `import` lines, read as source.

    Naming a module it does not import would sort a real absence as broken; omitting one it does
    import would sort a broken image as absent.
    """
    import ast
    from pathlib import Path

    root = Path(registry.__file__).parent
    wrong: list[str] = []
    for module in sorted(root.glob("*/*.py")):
        if module.name == "__init__.py":
            continue
        for node in ast.parse(module.read_text()).body:
            if not isinstance(node, ast.Try):
                continue
            imported = {
                alias.name.partition(".")[0]
                for statement in node.body
                if isinstance(statement, ast.Import)
                for alias in statement.names
            }
            for handler in node.handlers:
                for call in ast.walk(handler):
                    if not (
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Name)
                        and call.func.id == "mark_unavailable"
                    ):
                        continue
                    declared = next((k.value for k in call.keywords if k.arg == "optional"), None)
                    names = (
                        {e.value for e in declared.elts if isinstance(e, ast.Constant)}
                        if isinstance(declared, ast.Tuple)
                        else None
                    )
                    if names != imported:
                        wrong.append(f"{module.parent.name}/{module.name}: {names} != {imported}")
    assert not wrong, (
        "a predictor guard's `optional=` must name exactly the top-level modules its `try` "
        f"imports: {wrong}"
    )


def test_the_module_map_agrees_with_the_registry_names() -> None:
    """`_FORWARD_MODULES`/`_CONDITIONS_MODULES` name each predictor by its registry name.

    The map gives `discover_predictors`'s catch-all a name for a module it could not import and
    bounds `degradation.record`'s component label. On a checkout with no extras every predictor
    records itself under its class's own `name`, so registered plus unavailable names must equal
    what the map says.
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
    """A pod serving ten of eleven predictors is serving; taking it out is the larger harm.

    A missing checkpoint is an `OSError`, classified permanent, and a restart cannot recreate the
    file, so refusing on any permanent cause would crash-loop a working capability. The test below
    is the counterfactual: with no forward predictor left, the same broken entry is a refusal.
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
    """A kind with nothing left and something broken is unready.

    An enabled model that failed to load leaves `predict_forward_reaction` raising on every call. A
    developer checkout also has none; the difference is the cause: absent is a decision, broken is
    an image.
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
    """The catch-all files a broken module under its registry name, not its module name.

    `forward/reaction_t5` registers `reaction_t5_v2`, so a short-name key would hide the failure
    from an operator searching `/metrics` for the advertised id. The map-agreement test cannot see
    this, because in a normal checkout the catch-all never fires.
    """
    import importlib
    from types import ModuleType

    broken = "chemclaw_mcp_rxnpredict.engine.predictors.forward.reaction_t5"
    real = importlib.import_module

    def refuse(name: str, package: str | None = None) -> ModuleType:
        """Fail the way a module with a syntax error or a bad top-level import fails."""
        if name == broken:
            raise RuntimeError("a top-level import this module does not guard")
        return real(name, package)

    # The registry imports `importlib` itself, so this is the same module object it reads.
    monkeypatch.setattr(importlib, "import_module", refuse)
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
