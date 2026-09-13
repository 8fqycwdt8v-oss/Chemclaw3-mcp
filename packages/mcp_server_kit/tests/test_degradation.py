"""The degradation vocabulary, and the two properties the rest of the fleet rests on it having.

`degradation.classify` is what decides whether a broken component takes a pod out of rotation
(`rxnlabel`'s and `rxnpredict`'s readiness checks read `PERMANENT_CAUSES`) and what label reaches an
unauthenticated `/metrics`. Both consequences are one function, so both are checked here rather than
only where they are consumed.

The ordering test is the one that matters. `EgressForbidden` subclasses `OSError` deliberately —
`egress.py` says so — which is exactly why a classifier written in the obvious order buries it: a
refusal and a connection reset would both come back `failed`, and the fleet's most important
degradation would be the one indistinguishable from a network blip. So the refusal here is a
**real** one, raised by the armed guard from a real `getaddrinfo`, not a constructed object.
"""

from __future__ import annotations

import socket

import pytest
from mcp_server_kit import degradation
from prometheus_client import REGISTRY


def _refusal() -> BaseException:
    """The exception the armed guard actually raises, obtained by tripping it."""
    try:
        socket.getaddrinfo("example.invalid", 443)
    except OSError as exc:
        return exc
    raise AssertionError("the guard did not refuse; the root conftest should keep it armed")


def test_a_real_egress_refusal_is_not_sorted_as_a_generic_failure() -> None:
    """The refusal and a plain `OSError` must not land in the same bucket."""
    refusal = _refusal()
    assert isinstance(refusal, OSError), "the premise: it is an OSError, which is why order matters"
    assert degradation.classify(refusal) == degradation.CAUSE_EGRESS_REFUSED
    assert degradation.classify(ConnectionResetError(104, "reset")) == degradation.CAUSE_FAILED


def test_an_out_of_memory_is_separated_from_a_broken_checkpoint() -> None:
    """The split a readiness check acts on: one is transient, the other is a pod to replace.

    `OutOfMemoryError` is matched by *type name* because torch's is a `RuntimeError` subclass and
    this package must not import torch to see it; the double below is that shape exactly.
    """

    class OutOfMemoryError(RuntimeError):
        """Named as torch names both `torch.OutOfMemoryError` and `torch.cuda.OutOfMemoryError`."""

    assert degradation.classify(MemoryError()) == degradation.CAUSE_RESOURCE_EXHAUSTED
    assert degradation.classify(OutOfMemoryError("CUDA")) == degradation.CAUSE_RESOURCE_EXHAUSTED
    assert degradation.classify(RuntimeError("weights")) == degradation.CAUSE_FAILED
    assert degradation.CAUSE_RESOURCE_EXHAUSTED not in degradation.PERMANENT_CAUSES
    assert degradation.CAUSE_FAILED in degradation.PERMANENT_CAUSES


def test_a_missing_distribution_is_not_a_fault() -> None:
    """An optional extra nobody installed is a deployment's decision, not a broken image."""
    assert degradation.classify(ModuleNotFoundError("torch")) == degradation.CAUSE_NOT_INSTALLED
    assert degradation.CAUSE_NOT_INSTALLED not in degradation.PERMANENT_CAUSES


def test_an_unclamped_cause_is_refused_rather_than_published() -> None:
    """`/metrics` is unauthenticated, so a label nothing bounds is a series generator."""
    with pytest.raises(ValueError) as refused:
        degradation.record(server="kit", component="probe", cause="whatever-just-happened")
    assert "whatever-just-happened" in str(refused.value)
    assert (
        REGISTRY.get_sample_value(
            "chemclaw_mcp_degraded_total",
            {"server": "kit", "component": "probe", "cause": "whatever-just-happened"},
        )
        is None
    ), "the refused cause must not have minted a series on the way out"


def test_recording_moves_the_series_an_operator_scrapes() -> None:
    """The whole point: a degradation is visible from a scrape rather than from a log line."""
    labels = {"server": "kit", "component": "probe", "cause": degradation.CAUSE_FAILED}
    before = REGISTRY.get_sample_value("chemclaw_mcp_degraded_total", labels) or 0.0
    degradation.record(server="kit", component="probe", cause=degradation.CAUSE_FAILED)
    after = REGISTRY.get_sample_value("chemclaw_mcp_degraded_total", labels)
    assert after == before + 1.0
