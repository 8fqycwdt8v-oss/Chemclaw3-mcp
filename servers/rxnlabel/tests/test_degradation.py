"""A broken component must be tellable from an absent one, in the answer and from a scrape.

An empty `Naming()` is also the commonest correct answer, so a namer swallowed by `except`
would report "nothing matched" under a healthy version stamp that no drain re-derives. Each test
installs a component that constructs and then raises (a corrupt checkpoint, a guard-refused
loader) by filling the module's own cache slots, so the code under test is the code that runs.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from chemclaw_mcp_rxnlabel import app as app_module
from chemclaw_mcp_rxnlabel import tools
from chemclaw_mcp_rxnlabel.engine import mapping, naming, readiness, version
from mcp_server_kit import degradation
from prometheus_client import REGISTRY

_REACTION = "CC(=O)O.CCO>>CC(=O)OCC.O"


def _count(component: str, cause: str) -> float:
    """The degradation counter for one component and cause, 0 where the series is not yet minted."""
    return (
        REGISTRY.get_sample_value(
            "chemclaw_mcp_degraded_total",
            {"server": "rxnlabel", "component": component, "cause": cause},
        )
        or 0.0
    )


class _Exploding:
    """A mapper or namer that built fine and fails on every reaction."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def get_attention_guided_atom_maps(self, reactions: list[str], **kwargs: Any) -> Any:
        raise self._exc

    def __call__(self, reaction_smiles: str) -> Any:
        raise self._exc


@pytest.fixture
def broken_mapper(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fill `mapping`'s process-wide slot with a mapper that raises, and put it back after."""
    saved = (mapping._MAPPER, mapping._TRIED, mapping._FAILURE)
    mapping._MAPPER = _Exploding(getattr(request, "param", RuntimeError("corrupt checkpoint")))
    mapping._TRIED = True
    readiness.forget_verdict()
    try:
        yield
    finally:
        mapping._MAPPER, mapping._TRIED, mapping._FAILURE = saved
        readiness.forget_verdict()


@pytest.fixture
def broken_namer() -> Iterator[None]:
    """The same for `naming`."""
    saved = (naming._NAMER, naming._TRIED, naming._FAILURE)
    naming._NAMER = _Exploding(RuntimeError("corrupt SMIRKS table"))
    naming._TRIED = True
    readiness.forget_verdict()
    try:
        yield
    finally:
        naming._NAMER, naming._TRIED, naming._FAILURE = saved
        readiness.forget_verdict()


async def _healthz() -> httpx.Response:
    """GET `/healthz` on the real app over ASGI, without running its lifespan.

    This is what a kubelet calls: status code, redaction, memo and single-flight lock included,
    which calling `readiness.verify_labeller()` directly would miss. The memo TTL is zeroed by the
    fixture below.
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_module.app), base_url="http://rxnlabel.test"
    ) as client:
        return await client.get("/healthz")


@pytest.fixture(autouse=True)
def no_readiness_memo(monkeypatch: pytest.MonkeyPatch) -> None:
    """`connector_app` believes a failure for five seconds; these tests must not inherit one."""
    monkeypatch.setattr("mcp_server_kit.app.READINESS_FAILURE_TTL_SECONDS", 0.0)


def test_a_namer_that_raises_is_not_a_reaction_that_matched_nothing(broken_namer: None) -> None:
    """The two answers that used to be the same object, and the stamp that keeps them apart."""
    before = _count(naming.COMPONENT, degradation.CAUSE_FAILED)

    answers = tools._name([tools.NamingRequest(id="1", reaction_smiles=_REACTION)])

    (answer,) = answers
    assert answer.named_reaction is None, "the premise: the nulls look exactly like a clean miss"
    assert answer.degraded == [naming.COMPONENT], "and this is what says they are not"
    assert _count(naming.COMPONENT, degradation.CAUSE_FAILED) == before + 1.0
    # The stamp must not equal what a healthy pod of this build writes, or the row never re-labels.
    assert answer.version != version.labeller_version()


def test_a_mapper_that_raises_does_not_stamp_the_row_as_a_deployment_without_one(
    broken_mapper: None,
) -> None:
    """`mapped_smiles: null` is also what "no mapper installed" looks like. The stamp separates."""
    before = _count(mapping.COMPONENT, degradation.CAUSE_FAILED)

    (answer,) = tools._represent(
        [tools.ReactionRequest(id="1", reaction_smiles=_REACTION, species=["CCO"])]
    )

    assert answer.mapped_smiles is None, "the premise: indistinguishable from an absent mapper"
    assert answer.degraded == [mapping.COMPONENT]
    assert _count(mapping.COMPONENT, degradation.CAUSE_FAILED) == before + 1.0
    assert answer.version != version.labeller_version()
    assert answer.version != version.labeller_version(failed=[naming.COMPONENT]), (
        "the stamp has to name which component failed, not merely that one did"
    )


def test_a_refusal_by_the_egress_guard_is_counted_as_one(broken_namer: None) -> None:
    """An `EgressForbidden` is an `OSError`, so nothing downstream would otherwise show it."""
    from mcp_server_kit.egress import EgressForbidden

    naming._NAMER = _Exploding(EgressForbidden("huggingface.co"))
    before = _count(naming.COMPONENT, degradation.CAUSE_EGRESS_REFUSED)

    assert naming.name(_REACTION).failure == degradation.CAUSE_EGRESS_REFUSED
    assert _count(naming.COMPONENT, degradation.CAUSE_EGRESS_REFUSED) == before + 1.0


async def test_a_component_that_raises_on_the_probe_takes_the_pod_out_of_rotation(
    broken_namer: None,
) -> None:
    """A component that imports but raises on inference takes the pod out of rotation.

    `naming.available()` answers the import, not the inference. Driven through the served route, so
    the 503 and its redacted body are what is asserted.
    """
    assert naming.available(), "the premise: a broken namer still reports as present"
    response = await _healthz()
    assert response.status_code == 503
    assert "reaction namer" in response.json()["reason"]


@pytest.mark.parametrize("broken_mapper", [MemoryError("CUDA out of memory")], indirect=True)
async def test_a_pod_that_ran_out_of_memory_is_counted_and_left_alone(broken_mapper: None) -> None:
    """A memory spike is transient: counted, reported, and no traffic shed.

    The probe runs a forward pass heavier than most traffic it gates, so a failure there is evidence
    about the probe rather than the calls. Driven through `/healthz`, so the 200 is what a kubelet
    is told.
    """
    before = _count(mapping.COMPONENT, degradation.CAUSE_RESOURCE_EXHAUSTED)

    assert mapping.map_reaction(_REACTION).failure == degradation.CAUSE_RESOURCE_EXHAUSTED
    assert _count(mapping.COMPONENT, degradation.CAUSE_RESOURCE_EXHAUSTED) == before + 1.0
    response = await _healthz()
    assert response.status_code == 200, "a transient fault must not take the pod out of rotation"
    assert response.json()["datasets"] == []
