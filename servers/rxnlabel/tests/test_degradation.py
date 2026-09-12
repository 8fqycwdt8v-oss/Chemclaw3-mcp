"""A broken component must be tellable from an absent one — in the answer, and from a scrape.

Three `except Exception` blocks in this server turned a fault into a plausible answer. The namer's
was the worst of them: `Naming()` is byte-identical to the commonest *correct* answer this server
gives ("most of a patent corpus has no name"), so a pod whose rule table had gone reported nothing
matched for a whole corpus, under a `labeller_version` carrying the namer's own version number —
which means no drain would ever re-derive those rows, because their stored stamp already equalled a
healthy pod's. Measured before this file existed: `/healthz` **200**, the answer
`{"named_reaction": null, ..., "version": "...:namer@0.1.3"}`, and **no** `chemclaw_mcp_degraded_*`
series on `/metrics` at all.

Every test here installs a component that *constructs* and then raises, which is the shape of a
corrupt checkpoint and of a loader the egress guard refuses. The module's own cache slots are filled
rather than `map_reaction`/`name` being replaced, so the code under test is the code that runs.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
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
    saved = (mapping._MAPPER, mapping._TRIED)
    mapping._MAPPER = _Exploding(getattr(request, "param", RuntimeError("corrupt checkpoint")))
    mapping._TRIED = True
    readiness.verify_labeller.cache_clear()
    try:
        yield
    finally:
        mapping._MAPPER, mapping._TRIED = saved
        readiness.verify_labeller.cache_clear()


@pytest.fixture
def broken_namer() -> Iterator[None]:
    """The same for `naming`."""
    saved = (naming._NAMER, naming._TRIED)
    naming._NAMER = _Exploding(RuntimeError("corrupt SMIRKS table"))
    naming._TRIED = True
    readiness.verify_labeller.cache_clear()
    try:
        yield
    finally:
        naming._NAMER, naming._TRIED = saved
        readiness.verify_labeller.cache_clear()


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


def test_a_component_that_raises_on_the_probe_takes_the_pod_out_of_rotation(
    broken_namer: None,
) -> None:
    """Construction was the only thing checked, and it is the smaller half.

    A namer that imports and raises is a broken image; before this the probe passed it, because
    `naming.available()` answers the import rather than the inference.
    """
    assert naming.available(), "the premise: a broken namer still reports as present"
    with pytest.raises(RuntimeError) as unready:
        readiness.verify_labeller()
    assert "reaction namer" in str(unready.value)


@pytest.mark.parametrize("broken_mapper", [MemoryError("CUDA out of memory")], indirect=True)
def test_a_pod_that_ran_out_of_memory_is_counted_and_left_alone(broken_mapper: None) -> None:
    """The restart-storm case: readiness and liveness share `/healthz` in every Deployment here.

    A memory spike is a property of the moment, so it is counted and reported in the answer and is
    *not* a reason to take the pod out — which would restart it back into the same pressure.
    """
    before = _count(mapping.COMPONENT, degradation.CAUSE_RESOURCE_EXHAUSTED)

    assert mapping.map_reaction(_REACTION).failure == degradation.CAUSE_RESOURCE_EXHAUSTED
    assert _count(mapping.COMPONENT, degradation.CAUSE_RESOURCE_EXHAUSTED) == before + 1.0
    assert readiness.verify_labeller() == (), "a transient fault must not restart the pod"
