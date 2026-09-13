"""A degraded answer, counted and classified — because until this existed none of them were.

Three `except Exception` blocks in this fleet turned a broken component into a *plausible* answer:
`rxnlabel`'s mapper raising became "no atom map", its namer raising became "nothing matched", and a
`rxnpredict` predictor module that would not import became a quieter ensemble. Each logged a line
and none of them moved a number, so from a scrape a pod whose weights had vanished was
indistinguishable from a pod being asked easy questions.

**`EgressForbidden` is the reason this is a vocabulary rather than a boolean.** It subclasses
`OSError` precisely so it surfaces where a connection error would, which means any library's own
`except OSError: retry` swallows it whole — and `egress.py` says as much. A refusal that arrives
inside a model's loader is therefore the single most likely way this fleet's no-egress posture
becomes invisible: the counter `egress.py` increments fires, the *call* still returns something, and
the answer carries no trace. Classifying it here puts the same fact on the degradation the caller
actually sees.

**The cause is a closed set, and that is the label rule rather than a preference.** `/metrics` is
unauthenticated (`CLAUDE.md`, and `metrics.py` at length): no actor, no session, no correlation id,
no tool argument may be a label. A cause drawn from `CAUSES` is bounded by this file; a
`type(exc).__name__` folded straight into a label would be bounded by whatever a dependency decides
to raise, which is not a bound anybody here controls. `record` refuses an unclamped cause rather
than minting a series for it.

**What `classify` deliberately does not do is read a message.** A CUDA out-of-memory arrives as
`torch.cuda.OutOfMemoryError`, a *subclass of `RuntimeError`* whose text has changed across
releases; matching on that text would be a control that works until upstream rewords itself. So the
resource
branch matches the exception's **type name** against a two-element set — a name is a stable API
surface in a way a message is not — and every torch failure that is not one of those two is counted
as `failed` on purpose, with the `repr` in the log beside it. A wrong cause is worse than a coarse
one: `resource_exhausted` is the cause `rxnlabel`'s readiness check refuses to treat as a reason to
leave the pod, so anything mis-sorted into it would be a defect that stops a broken pod being
replaced.
"""

from __future__ import annotations

from prometheus_client import Counter

from mcp_server_kit.egress import EgressForbidden

__all__ = [
    "CAUSES",
    "CAUSE_EGRESS_REFUSED",
    "CAUSE_FAILED",
    "CAUSE_NOT_INSTALLED",
    "CAUSE_RESOURCE_EXHAUSTED",
    "DEGRADED",
    "PERMANENT_CAUSES",
    "classify",
    "record",
]

# The in-process egress guard refused an outbound call this component needed. Permanent by
# construction: a NetworkPolicy and an armed guard do not change under load, so a component that
# reaches for the network at request time is a component that will never work in this deployment.
CAUSE_EGRESS_REFUSED = "egress_refused"

# The process ran out of something it can get back: memory, a device allocation. **The one cause
# that is transient**, and the one a readiness check must not act on — see `PERMANENT_CAUSES`.
CAUSE_RESOURCE_EXHAUSTED = "resource_exhausted"

# The component's distribution is not in this image. A deployment's decision, not a fault: this
# fleet's optional components are optional by design and say so in the answer.
CAUSE_NOT_INSTALLED = "not_installed"

# Everything else: a checkpoint that will not parse, a corrupt table, a library raising on an input
# it cannot tokenise. Coarse on purpose — see the module docstring on why the resource branch is
# narrow rather than generous.
CAUSE_FAILED = "failed"

CAUSES = frozenset(
    {CAUSE_EGRESS_REFUSED, CAUSE_RESOURCE_EXHAUSTED, CAUSE_NOT_INSTALLED, CAUSE_FAILED}
)

# The causes that mean *this pod will keep answering wrongly until it is replaced*. A readiness
# check may act on these and must not act on the others, because a Deployment in this repository
# points `readinessProbe` and `livenessProbe` at the same `/healthz`: a signal that flips under load
# does not shed traffic, it restarts the pod, and a fleet-wide spike becomes a fleet-wide restart
# storm. `CAUSE_NOT_INSTALLED` is excluded for the opposite reason — an absent optional component is
# the deployment working as designed.
PERMANENT_CAUSES = frozenset({CAUSE_EGRESS_REFUSED, CAUSE_FAILED})

# Exception *type names* that mean the process ran out of a resource. `MemoryError` is CPython's;
# `OutOfMemoryError` is the name torch gives both `torch.OutOfMemoryError` and
# `torch.cuda.OutOfMemoryError`, which subclass `RuntimeError` and so cannot be caught by type here
# without importing torch into a package that must not depend on it.
_RESOURCE_TYPE_NAMES = frozenset({"MemoryError", "OutOfMemoryError"})

DEGRADED = Counter(
    "chemclaw_mcp_degraded_total",
    "Answers returned with a component's contribution missing, by component and cause.",
    ("server", "component", "cause"),
)


def classify(exc: BaseException) -> str:
    """Which `CAUSES` member `exc` is, checked most specific first.

    `EgressForbidden` is tested before anything else deliberately: it is an `OSError`, so any
    branch that sorted `OSError` first would bury the one cause this fleet most needs to see.

    Args:
        exc: The exception a component raised.

    Returns:
        One of `CAUSES`.
    """
    if isinstance(exc, EgressForbidden):
        return CAUSE_EGRESS_REFUSED
    if type(exc).__name__ in _RESOURCE_TYPE_NAMES:
        return CAUSE_RESOURCE_EXHAUSTED
    if isinstance(exc, ImportError):
        return CAUSE_NOT_INSTALLED
    return CAUSE_FAILED


def record(*, server: str, component: str, cause: str) -> None:
    """Count one degraded answer.

    Args:
        server: The `connector_app` name of the server reporting it.
        component: What went missing. A module-level constant at every call site, never a value
            derived from a request — the label rule in `metrics.py` is why.
        cause: A member of `CAUSES`.

    Raises:
        ValueError: `cause` is not one of `CAUSES`. Refused rather than recorded, because an
            unclamped label on an unauthenticated endpoint is a series per string whoever reaches
            the pod invents.
    """
    if cause not in CAUSES:
        raise ValueError(
            f"{cause!r} is not a degradation cause; `/metrics` is unauthenticated, so this label "
            f"is clamped to {sorted(CAUSES)}"
        )
    DEGRADED.labels(server, component, cause).inc()
