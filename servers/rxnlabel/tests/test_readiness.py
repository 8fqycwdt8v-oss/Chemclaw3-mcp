"""`/healthz` separates "this deployment chose not to install a model" from "it broke".

The mapper and namer are optional by design and recorded in `labeller_version`, so a
distribution that is not there is a choice, while one that is there and will not construct is a
broken image that must not keep writing coarse labels. Driven through the served route over
ASGI without the lifespan, so the status code, memo, redaction and single-flight lock that
`connector_app` adds are all exercised.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from chemclaw_mcp_rxnlabel import app as app_module
from chemclaw_mcp_rxnlabel.engine import mapping, naming, readiness, species, version
from mcp_server_kit import degradation

_DEPLOYMENT_PATH = Path(__file__).resolve().parents[1] / "deploy" / "deployment.yaml"


@pytest.fixture(autouse=True)
def fresh_verdict(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Start each test with no cached verdict at either layer, and restore the module's state.

    Both caches are process-wide: `readiness`'s verdict and `connector_app`'s failure memo, whose
    TTL is zeroed rather than slept through.
    """
    monkeypatch.setattr("mcp_server_kit.app.READINESS_FAILURE_TTL_SECONDS", 0.0)
    saved = (mapping._MAPPER, mapping._TRIED, mapping._FAILURE, mapping._ATTEMPTED_AT)
    saved_namer = (naming._NAMER, naming._TRIED, naming._FAILURE, naming._ATTEMPTED_AT)
    saved_detail = (mapping._FAILURE_DETAIL, naming._FAILURE_DETAIL)
    readiness.forget_verdict()
    try:
        yield
    finally:
        mapping._MAPPER, mapping._TRIED, mapping._FAILURE, mapping._ATTEMPTED_AT = saved
        naming._NAMER, naming._TRIED, naming._FAILURE, naming._ATTEMPTED_AT = saved_namer
        mapping._FAILURE_DETAIL, naming._FAILURE_DETAIL = saved_detail
        readiness.forget_verdict()


async def _probe() -> httpx.Response:
    """GET `/healthz` on the real app, over ASGI and without running its lifespan."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_module.app), base_url="http://rxnlabel.test"
    ) as client:
        return await client.get("/healthz")


async def test_readiness_labels_a_fixture_reaction_through_the_engine() -> None:
    """The probe exercises the labelling path, not an import — a broken rule table must fail it."""
    response = await _probe()
    assert response.status_code == 200
    assert response.json()["datasets"] == [], "this server vendors no corpus, and says so"


async def test_an_uninstalled_component_is_ready_and_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No mapper installed is this deployment's decision, and the server answers with it."""
    monkeypatch.setattr(mapping, "available", lambda: False)
    monkeypatch.setattr(naming, "available", lambda: False)
    monkeypatch.setattr(version, "_installed", lambda _name: "absent")
    assert (await _probe()).status_code == 200


async def test_an_installed_component_that_will_not_construct_is_unready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A checkpoint that failed to load is a broken image, and it must not take traffic."""
    monkeypatch.setattr(mapping, "available", lambda: False)
    monkeypatch.setattr(version, "_installed", lambda name: "9.9.9")
    response = await _probe()
    assert response.status_code == 503
    assert "rxnmapper" in response.json()["reason"]


async def test_an_unbuilt_component_is_unready_whatever_the_cause_said(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An installed component that failed to construct is unready whatever the cause.

    A transient cause normally keeps a pod in rotation, but an unbuilt mapper stamps every row
    `mapper@absent`, identical to a deployment that never installed one. So this branch refuses
    regardless, and the cause decides only whether the refusal can lift without a restart.
    """
    monkeypatch.setattr(mapping, "available", lambda: False)
    monkeypatch.setattr(mapping, "construction_failure", lambda: "resource_exhausted")
    monkeypatch.setattr(version, "_installed", lambda name: "9.9.9")
    assert degradation.CAUSE_RESOURCE_EXHAUSTED not in degradation.PERMANENT_CAUSES, "the premise"
    response = await _probe()
    assert response.status_code == 503
    assert "resource_exhausted" in response.json()["reason"], (
        "the refusal must name the cause the construction attempt recorded; it was classified, "
        "counted and then discarded, so the 503 could not say whether this pod needed replacing "
        "or a moment"
    )
    assert "mapper@absent" not in response.json()["reason"]


def test_a_transient_construction_failure_is_retried_rather_than_latched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient construction failure is retried after its window, not latched for the process life.

    Otherwise the 503 above is a restart dressed as readiness.
    """
    attempts: list[str] = []

    def build() -> object:
        """Fail once the way a busy pod does, then succeed the way a recovered one does."""
        attempts.append("tried")
        if len(attempts) == 1:
            raise MemoryError("Unable to allocate 48.0 MiB for an array")
        return object()

    monkeypatch.setattr(mapping, "CONSTRUCTION_RETRY_SECONDS", 0.0)
    _install_rxnmapper(monkeypatch, build)
    assert mapping._mapper() is None and mapping._FAILURE == degradation.CAUSE_RESOURCE_EXHAUSTED
    assert mapping._mapper() is not None, (
        "a transient construction failure that is never retried makes the 503 above terminal, and "
        "only a restart can clear it"
    )
    assert attempts == ["tried", "tried"] and mapping._FAILURE is None


def test_a_permanent_construction_failure_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counterfactual: re-reading a corrupt checkpoint learns nothing and costs CPU."""
    attempts: list[str] = []

    def build() -> object:
        """A checkpoint that will not parse, however many times it is read."""
        attempts.append("tried")
        raise RuntimeError("checkpoint is truncated")

    monkeypatch.setattr(mapping, "CONSTRUCTION_RETRY_SECONDS", 0.0)
    _install_rxnmapper(monkeypatch, build)
    assert mapping._mapper() is None and mapping._FAILURE == degradation.CAUSE_FAILED
    assert mapping._mapper() is None
    assert attempts == ["tried"], (
        f"a permanent cause was re-attempted {len(attempts)} times; the retry window is for a "
        "cause that can improve"
    )


def test_the_namer_retries_a_transient_construction_failure_like_the_mapper_does() -> None:
    """The namer retries a transient construction failure the way the mapper does.

    A `MemoryError` or `EMFILE` while importing `rxn_insight` must not take the namer out for the
    life of the process. The retry predicate is `engine/construction.py`'s, shared with `mapping`.
    """
    attempts: list[str] = []

    def reaction_class() -> object:
        """Fail once the way a pod under memory pressure does, then succeed."""
        attempts.append("tried")
        if len(attempts) == 1:
            raise MemoryError("Unable to allocate 48.0 MiB for an array")
        return object

    with _rxn_insight(reaction_class, retry_seconds=0.0):
        assert naming._namer() is None
        assert naming._FAILURE == degradation.CAUSE_RESOURCE_EXHAUSTED
        assert naming._namer() is not None, (
            "a transient import failure that is never retried makes the namer absent for the life "
            "of the process, and only a restart can clear it"
        )
        assert attempts == ["tried", "tried"]
        assert naming._FAILURE is None, "the cause was not cleared by the attempt that succeeded"
        assert naming.available()


def test_the_namer_does_not_retry_a_permanent_cause_or_an_absent_extra() -> None:
    """The namer does not retry a permanent cause or an absent extra.

    A rule table that will not parse stays broken, and an uninstalled extra is a deployment's
    choice (`_FAILURE` stays `None`, which `construction.retry_due` reads as nothing to retry); a
    retry on everything is a busy loop.
    """
    permanent: list[str] = []

    def truncated() -> object:
        permanent.append("tried")
        raise RuntimeError("the SMIRKS table is truncated")

    with _rxn_insight(truncated, retry_seconds=0.0):
        for _ in range(3):
            assert naming._namer() is None
        assert naming._FAILURE == degradation.CAUSE_FAILED
        assert permanent == ["tried"], (
            f"a permanent cause was re-attempted {len(permanent)} times; the retry window is for a "
            "cause that can improve"
        )

    absent: list[str] = []

    def not_installed() -> object:
        absent.append("tried")
        # What `from rxn_insight.reaction import ...` raises with the package absent: the import
        # system names the top-level package, not the submodule.
        raise ModuleNotFoundError("No module named 'rxn_insight'", name="rxn_insight")

    with _rxn_insight(not_installed, retry_seconds=0.0):
        for _ in range(3):
            assert naming._namer() is None
        assert naming._FAILURE is None, "an extra that is not installed is not a failure"
        assert absent == ["tried"], "an absent extra was re-imported, which learns nothing"

    # The window is honoured: the other arms shorten it to zero, so a gate ignoring `window_seconds`
    # would pass them while re-loading the rule table on every request.
    busy: list[str] = []

    def transient() -> object:
        busy.append("tried")
        raise MemoryError("Unable to allocate 48.0 MiB for an array")

    with _rxn_insight(transient, retry_seconds=3600.0):
        for _ in range(3):
            assert naming._namer() is None
        assert naming._FAILURE == degradation.CAUSE_RESOURCE_EXHAUSTED
        assert busy == ["tried"], (
            f"a transient cause was re-attempted {len(busy)} times inside a 3600 s window, so the "
            "retry is a per-call loop rather than a bounded one"
        )


async def test_a_namer_whose_shared_library_will_not_load_is_broken_not_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An installed namer whose shared library will not load is broken, not absent.

    A plain `ImportError` means the module was found and would not load, so it is `failed` and the
    refusal carries the library's own message, rather than reading as an uninstalled extra.
    """

    def broken() -> object:
        raise ImportError("libcudart.so.11.0: cannot open shared object file: No such file")

    monkeypatch.setattr(
        version, "_installed", lambda name: "0.1.3" if name == "rxn-insight" else "absent"
    )
    with _rxn_insight(broken, retry_seconds=3600.0):
        assert naming._namer() is None
        assert naming._FAILURE == degradation.CAUSE_FAILED, (
            "an installed distribution that will not load was sorted as not installed"
        )
        assert naming._FAILURE in degradation.PERMANENT_CAUSES
        response = await _probe()
    assert response.status_code == 503
    reason = response.json()["reason"]
    assert "rxn-insight" in reason and "libcudart.so.11.0" in reason, (
        f"the refusal must say what broke, not only which bucket it fell in: {reason}"
    )


def test_a_mapper_missing_a_dependency_of_its_own_is_broken_not_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other shape: `ModuleNotFoundError`, but for a module that is not the extra.

    An installed `rxnmapper` whose `transformers` is gone raises exactly the type an absent extra
    does. Only the *name* separates them, and only this module knows which name it tolerates.
    """

    def build() -> object:
        raise ModuleNotFoundError("No module named 'transformers'", name="transformers")

    monkeypatch.setattr(mapping, "CONSTRUCTION_RETRY_SECONDS", 3600.0)
    _install_rxnmapper(monkeypatch, build)
    assert mapping._mapper() is None
    assert mapping._FAILURE == degradation.CAUSE_FAILED
    assert "transformers" in (mapping.construction_detail() or "")


def test_a_mapper_that_is_not_installed_is_still_absent_rather_than_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counterfactual that keeps a developer checkout ready: the extra's own name is absent."""
    # `None` in `sys.modules` is the import system's own spelling of "not importable": it raises
    # the `ModuleNotFoundError` an absent distribution does, with `name` set, whatever is on disk.
    monkeypatch.setitem(sys.modules, "rxnmapper", None)
    mapping._MAPPER, mapping._TRIED, mapping._FAILURE = None, False, None
    assert mapping._mapper() is None
    assert mapping._FAILURE is None, "an extra nobody installed is not a failure"
    assert mapping.construction_detail() is None


@contextmanager
def _rxn_insight(reaction_class: Callable[[], object], *, retry_seconds: float) -> Iterator[None]:
    """Stand a fake `rxn_insight.reaction` in front of `naming._namer`, and clear the latch.

    `reaction_class` is called on each attempt, so a test can count attempts and fail one;
    `naming._namer` imports `Reaction` by name, so the module attribute is the seam.
    """

    def attribute(_self: types.ModuleType, name: str) -> object:
        """`Reaction` is the one name `_namer` reaches for; anything else is the import machinery.

        The machinery asks for `__path__` on the way in, so intercepting every name would count two
        extra attempts per import and make the attempt count meaningless.
        """
        if name != "Reaction":
            raise AttributeError(name)
        return reaction_class()

    module = types.ModuleType("rxn_insight")
    reaction = types.ModuleType("rxn_insight.reaction")
    reaction.__class__ = type(
        "AttemptCountingModule", (types.ModuleType,), {"__getattr__": attribute}
    )
    saved_window = naming.CONSTRUCTION_RETRY_SECONDS
    saved_modules = {
        name: sys.modules.get(name) for name in ("rxn_insight", "rxn_insight.reaction")
    }
    sys.modules["rxn_insight"] = module
    sys.modules["rxn_insight.reaction"] = reaction
    naming.CONSTRUCTION_RETRY_SECONDS = retry_seconds
    naming._NAMER, naming._TRIED, naming._FAILURE, naming._ATTEMPTED_AT = None, False, None, None
    naming._FAILURE_DETAIL = None
    try:
        yield
    finally:
        naming.CONSTRUCTION_RETRY_SECONDS = saved_window
        for name, was in saved_modules.items():
            if was is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = was


def _install_rxnmapper(monkeypatch: pytest.MonkeyPatch, build: object) -> None:
    """Put `build` where `mapping._mapper` imports `RXNMapper` from, and clear the latch."""
    import sys
    import types

    module = types.ModuleType("rxnmapper")
    module.RXNMapper = build  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rxnmapper", module)
    mapping._MAPPER = None
    mapping._TRIED = False
    mapping._FAILURE = None
    mapping._FAILURE_DETAIL = None
    mapping._ATTEMPTED_AT = None


async def test_a_component_that_breaks_after_a_good_probe_stops_reporting_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verdict expires, so a component that breaks after a good probe stops reporting ready.

    The realistic failure is later, not at startup: a weight file truncated on a mount, a rule
    table on a remounted share.
    """
    assert (await _probe()).status_code == 200
    assert (await _probe()).status_code == 200, "the verdict is cached, which is the point of it"

    monkeypatch.setattr(
        naming,
        "name",
        lambda _reaction: naming.Naming(failure=degradation.CAUSE_FAILED),
    )
    assert (await _probe()).status_code == 200, (
        "still cached: the window is what bounds the fixture's cost, and it is asserted here so "
        "that shrinking it to zero is a deliberate change rather than an accident"
    )

    monkeypatch.setattr(readiness, "VERDICT_TTL_SECONDS", 0.0)
    readiness.forget_verdict()
    broken = await _probe()
    assert broken.status_code == 503, (
        "a component that broke after the first successful probe reported ready for the life of "
        "the process"
    )
    assert "reaction namer" in broken.json()["reason"]


async def test_a_transient_failure_of_the_labelling_path_keeps_the_pod_in_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient failure of the always-required labelling path keeps the pod in service.

    `roles.assign` and the species helpers allocate on every cold probe, so a memory failure there
    is about the probe, not the pod. Classification is `connector_app`'s, so the component label is
    `readiness`; `degraded` in the body proves the funnel ran.
    """
    labels = {
        "server": "rxnlabel",
        "component": "readiness",
        "cause": degradation.CAUSE_RESOURCE_EXHAUSTED,
    }
    from prometheus_client import REGISTRY

    before = REGISTRY.get_sample_value("chemclaw_mcp_degraded_total", labels) or 0.0

    calls: list[str] = []

    def out_of_memory(_smiles: str) -> list[str]:
        """Fail the way RDKit does when the pod is at its ceiling."""
        calls.append("tried")
        raise MemoryError("Unable to allocate array")

    monkeypatch.setattr(species, "functional_groups", out_of_memory)
    response = await _probe()
    assert response.status_code == 200, (
        "a transient failure of the labelling path must shed no traffic; the counter is what it "
        "gets instead"
    )
    assert response.json()["degraded"] == degradation.CAUSE_RESOURCE_EXHAUSTED
    assert "datasets" not in response.json(), "nothing was verified, so nothing may be claimed"
    assert REGISTRY.get_sample_value("chemclaw_mcp_degraded_total", labels) == before + 1.0

    # The raise is cached too, which bounds the cost: otherwise every kubelet probe would re-run an
    # unadmitted RXNMapper forward pass for as long as the fault lasted.
    assert (await _probe()).status_code == 200
    assert calls == ["tried"], (
        f"the failing probe ran {len(calls)} times for two /healthz calls; an exception outside "
        "the verdict window is a transformer forward pass per probe interval, for ever"
    )


async def test_a_permanent_failure_of_the_labelling_path_sheds_traffic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counterfactual, so the test above is about the cause rather than about the route."""

    def corrupt(_smiles: str) -> list[str]:
        """Fail the way a corrupt SMARTS vocabulary does."""
        raise RuntimeError("the functional-group vocabulary will not compile")

    monkeypatch.setattr(species, "functional_groups", corrupt)
    response = await _probe()
    assert response.status_code == 503
    assert "vocabulary" in response.json()["reason"]


def test_the_app_wires_the_probe_in() -> None:
    """A readiness callable nothing passes to `connector_app` is a control that does not exist."""
    assert app_module._readiness is not None
    assert app_module._readiness() == []


def test_the_verdict_window_is_bounded_by_the_probe_cadence_this_server_declares() -> None:
    """The shipped verdict window is bounded by the probe cadence this server declares.

    The tests above patch the window, so they prove the mechanism, not the number. Below one probe
    period the fixture's forward pass runs on nearly every probe; above a dozen a broken component
    keeps serving for minutes. The cadence is read from `deployment.yaml`, which is what a kubelet
    reads.
    """
    import yaml

    deployment = yaml.safe_load(_DEPLOYMENT_PATH.read_text(encoding="utf-8"))
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    period = int(container["readinessProbe"]["periodSeconds"])
    assert period > 0, f"{_DEPLOYMENT_PATH} declares no readiness period"
    assert period <= readiness.VERDICT_TTL_SECONDS <= 12 * period, (
        f"VERDICT_TTL_SECONDS is {readiness.VERDICT_TTL_SECONDS} against a {period}s readiness "
        f"period: below {period} the probe pays a transformer forward pass on every probe, above "
        f"{12 * period} a component that broke mid-life keeps serving for minutes"
    )
    assert period <= mapping.CONSTRUCTION_RETRY_SECONDS <= 12 * period, (
        f"CONSTRUCTION_RETRY_SECONDS is {mapping.CONSTRUCTION_RETRY_SECONDS}: below a probe period "
        "the weights are re-read on every probe, above a dozen a pod that could build them stays "
        "unready for minutes"
    )
