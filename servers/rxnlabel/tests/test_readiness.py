"""`/healthz` here has to separate "this deployment chose not to install a model" from "it broke".

`rxnlabel`'s two heavy components are optional *by design*: without them a reaction is labelled
without an atom map and without a name, `engine/version.py` records that in `labeller_version`, and
the corpus re-labels itself the day they arrive. That design is what made the readiness gap easy to
miss — "optional" was read as "nothing to be unready about", so this server passed no `readiness=`
at all and answered a constant `{"status": "ok"}`. A pod whose `rxnmapper` checkpoint failed to load
therefore passed its probe, took traffic, and quietly wrote coarse labels under a version string
that claimed no mapper was ever installed.

The distinction this module has to make is the one `version._installed` already knows how to make:
a distribution that is *not there* is a deployment's choice, and a distribution that is there and
will not construct is a broken image.

**Driven through the served route rather than through the probe function**, which is what these
tests did before and is weaker than it reads: `verify_labeller()` is not what a kubelet calls, and
calling it directly misses the status code, the five-second memo, the redaction and the
single-flight
lock that `connector_app` wraps it in — every one of which has had a defect in it. The app object is
driven over ASGI without its lifespan, because `/healthz` needs no session manager and
`StreamableHTTPSessionManager.run()` may be called only once per instance, which `test_server.py`
spends on its own uvicorn.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from chemclaw_mcp_rxnlabel import app as app_module
from chemclaw_mcp_rxnlabel.engine import mapping, readiness
from mcp_server_kit import degradation


@pytest.fixture(autouse=True)
def fresh_verdict(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Start each test with no cached verdict at either layer, and restore the module's state.

    Two caches sit in front of this probe and both are process-wide: `readiness`'s own verdict and
    `connector_app`'s memo of a failure. The memo's TTL is zeroed rather than slept through,
    which is
    the trick `packages/mcp_server_kit/tests/test_readiness.py` uses for the same reason.
    """
    monkeypatch.setattr("mcp_server_kit.app.READINESS_FAILURE_TTL_SECONDS", 0.0)
    saved = (mapping._MAPPER, mapping._TRIED, mapping._FAILURE, mapping._ATTEMPTED_AT)
    readiness.forget_verdict()
    try:
        yield
    finally:
        mapping._MAPPER, mapping._TRIED, mapping._FAILURE, mapping._ATTEMPTED_AT = saved
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
    monkeypatch.setattr(readiness.mapping, "available", lambda: False)
    monkeypatch.setattr(readiness.naming, "available", lambda: False)
    monkeypatch.setattr(readiness.version, "_installed", lambda _name: "absent")
    assert (await _probe()).status_code == 200


async def test_an_installed_component_that_will_not_construct_is_unready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A checkpoint that failed to load is a broken image, and it must not take traffic."""
    monkeypatch.setattr(readiness.mapping, "available", lambda: False)
    monkeypatch.setattr(readiness.version, "_installed", lambda name: "9.9.9")
    response = await _probe()
    assert response.status_code == 503
    assert "rxnmapper" in response.json()["reason"]


async def test_an_unbuilt_component_is_unready_whatever_the_cause_said(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 1a correction, and it goes the opposite way from the obvious reading of the rule.

    A mapper that is installed and failed to construct on a `MemoryError` reports a *transient*
    cause, and `PERMANENT_CAUSES` says a transient cause must not take a pod out of rotation.
    Applied
    here that is the worse answer, and measurably so: with the mapper unbuilt, `version._component`
    reads `available()` False and stamps every row `mapper@absent` — byte-identical to a deployment
    that never installed one, which is the stamp defect the degradation record exists to end. So
    this
    branch refuses whatever the cause, and the cause is used for something else entirely:
    whether the
    refusal can be lifted without a restart.
    """
    monkeypatch.setattr(readiness.mapping, "available", lambda: False)
    monkeypatch.setattr(readiness.mapping, "construction_failure", lambda: "resource_exhausted")
    monkeypatch.setattr(readiness.version, "_installed", lambda name: "9.9.9")
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
    """A 503 from the branch above has to be liftable, or it is a restart dressed as readiness.

    `_TRIED` latched on the first attempt and was never cleared, so a mapper that failed to build on
    one busy minute was absent for the life of the process: measured, `/healthz` answered 503 for
    ever and nothing in the process could change its mind.
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
    mapping._ATTEMPTED_AT = None


async def test_a_component_that_breaks_after_a_good_probe_stops_reporting_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verdict expires, because `lru_cache` made the first answer the only answer.

    `verify_labeller` was `lru_cache(maxsize=1)` and its docstring argued the cache was safe because
    "`lru_cache` does not cache exceptions, so a broken pod is re-probed and stays 503" — true of a
    pod broken at startup and false of one that breaks later, which is the realistic shape: a weight
    file truncated on a mount, a rule table on a remounted share. Driven on the real app: three
    probes 200, the namer then raising on every reaction, probe four **200**.
    """
    assert (await _probe()).status_code == 200
    assert (await _probe()).status_code == 200, "the verdict is cached, which is the point of it"

    monkeypatch.setattr(
        readiness.naming,
        "name",
        lambda _reaction: readiness.naming.Naming(failure=degradation.CAUSE_FAILED),
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
    """The funnel, on the half of the probe that is not optional at all.

    `roles.assign`, `species.canonical_smiles` and `species.functional_groups` are the
    never-optional
    path, and they allocate: this probe runs a real RXNMapper forward pass plus RDKit
    canonicalisation
    on every cold probe, so it is itself a plausible place for an allocation to fail under pressure.
    Every raise from them went through `connector_app`'s `except Exception` into an unconditional
    503 — so the probe's answer to failing on memory was "restart me", which at the time it was.
    """
    labels = {
        "server": "rxnlabel",
        "component": "labelling_path",
        "cause": degradation.CAUSE_RESOURCE_EXHAUSTED,
    }
    from prometheus_client import REGISTRY

    before = REGISTRY.get_sample_value("chemclaw_mcp_degraded_total", labels) or 0.0

    def out_of_memory(_smiles: str) -> list[str]:
        """Fail the way RDKit does when the pod is at its ceiling."""
        raise MemoryError("Unable to allocate array")

    monkeypatch.setattr(readiness.species, "functional_groups", out_of_memory)
    response = await _probe()
    assert response.status_code == 200, (
        "a transient failure of the labelling path must shed no traffic; the counter is what it "
        "gets instead"
    )
    assert REGISTRY.get_sample_value("chemclaw_mcp_degraded_total", labels) == before + 1.0


async def test_a_permanent_failure_of_the_labelling_path_sheds_traffic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counterfactual, so the test above is about the cause rather than about the route."""

    def corrupt(_smiles: str) -> list[str]:
        """Fail the way a corrupt SMARTS vocabulary does."""
        raise RuntimeError("the functional-group vocabulary will not compile")

    monkeypatch.setattr(readiness.species, "functional_groups", corrupt)
    response = await _probe()
    assert response.status_code == 503
    assert "vocabulary" in response.json()["reason"]


def test_the_app_wires_the_probe_in() -> None:
    """A readiness callable nothing passes to `connector_app` is a control that does not exist."""
    assert app_module._readiness is not None
    assert app_module._readiness() == []
