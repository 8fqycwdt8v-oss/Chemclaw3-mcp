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

**Both labels are closed sets, and that is the label rule rather than a preference.** `/metrics` is
unauthenticated (`CLAUDE.md`, and `metrics.py` at length): no actor, no session, no correlation id,
no tool argument may be a label. A cause drawn from `CAUSES` is bounded by this file; a
`type(exc).__name__` folded straight into a label would be bounded by whatever a dependency decides
to raise, which is not a bound anybody here controls. `component` is bounded by
`register_components`, which exists because for one wave it was bounded by nothing at all while this
paragraph read as though it were — `record` clamps an unregistered one onto `UNKNOWN_COMPONENT`
rather than minting a series for it, and clamps rather than raises for the reason `record` gives.

**What `classify` deliberately does not do is read a message.** A CUDA out-of-memory arrives as
`torch.cuda.OutOfMemoryError`, a *subclass of `RuntimeError`* whose text has changed across
releases; matching on that text would be a control that works until upstream rewords itself. So the
resource
branch matches the exception's **type name** against a two-element set — a name is a stable API
surface in a way a message is not — and every torch failure that is not one of those two is counted
as `failed` on purpose, with the `repr` in the log beside it. A wrong cause is worse than a coarse
one, and since liveness stopped reading `/healthz` the asymmetry is decidable rather than a matter
of taste: a cause wrongly sorted *into* `resource_exhausted` leaves a broken pod in service, while
one wrongly sorted out of it sheds traffic for one probe interval. The first is the expensive
mistake, so the resource branch stays narrow and explicit — two type names and four errno values,
each argued where it is written.
"""

from __future__ import annotations

import errno
import logging

from prometheus_client import Counter

from mcp_server_kit.egress import EgressForbidden

logger = logging.getLogger(__name__)

__all__ = [
    "CAUSES",
    "CAUSE_EGRESS_REFUSED",
    "CAUSE_FAILED",
    "CAUSE_NOT_INSTALLED",
    "CAUSE_RESOURCE_EXHAUSTED",
    "DEGRADED",
    "PERMANENT_CAUSES",
    "UNKNOWN_COMPONENT",
    "classify",
    "record",
    "register_components",
    "registered_components",
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
# check may act on these and must not act on the others, and `connector_app`'s `/healthz` now
# enforces that for every server rather than leaving each callable to remember it — which is how two
# of the seven came to answer 503 for a transient anyway.
#
# **The reason used to be that a 503 here restarted the pod, and that reason is gone.** Every
# Deployment pointed `readinessProbe` *and* `livenessProbe` at this one route, so an unready answer
# replaced the pod instead of shedding load;
# `D-2026-09-13-a-probe-that-can-kill-the-pod-is-not-a-readiness-probe` gave liveness its own
# `/livez` and a 503 here now means only "do not send me traffic", reversed by the next passing
# probe. What survives the decoupling is the narrower argument, and it is the one that matters for
# `rxnlabel`: its probe runs a transformer forward pass, which is *heavier* than most of the traffic
# it gates, so a probe that fails on an allocation is evidence about the probe rather than about the
# calls — shedding a pod that can still serve. `CAUSE_NOT_INSTALLED` is excluded for the opposite
# reason: an absent optional component is the deployment working as designed.
PERMANENT_CAUSES = frozenset({CAUSE_EGRESS_REFUSED, CAUSE_FAILED})

# Exception *type names* that mean the process ran out of a resource. `MemoryError` is CPython's;
# `OutOfMemoryError` is the name torch gives both `torch.OutOfMemoryError` and
# `torch.cuda.OutOfMemoryError`, which subclass `RuntimeError` and so cannot be caught by type here
# without importing torch into a package that must not depend on it.
_RESOURCE_TYPE_NAMES = frozenset({"MemoryError", "OutOfMemoryError"})

# `OSError.errno` values that mean the same thing the two type names above do: the process asked the
# kernel for something it can get back and was told no. **Added because the narrow type-name branch
# was narrower than its own comment claimed**: `CAUSE_RESOURCE_EXHAUSTED` describes "memory, a
# device allocation", and `OSError(ENOMEM, "Cannot allocate memory")` — literally that sentence,
# raised by every allocating syscall in libc — classified `failed` and therefore *permanent*, as did
# the two textbook load-induced transients `EMFILE`/`ENFILE` (a pod at its descriptor ceiling) and
# `EAGAIN` (a pod at its thread or process ceiling). Measured before this set existed: all four sat
# in the permanent bucket beside a corrupt checkpoint.
#
# `EWOULDBLOCK` is `EAGAIN` on Linux and is therefore already here. A bare `TimeoutError` carries no
# errno and stays `failed` deliberately — see `classify`.
_RESOURCE_ERRNOS = frozenset({errno.ENOMEM, errno.EMFILE, errno.ENFILE, errno.EAGAIN})

# The component label a name nothing registered collapses onto. The sentinel spelling `app.py` uses
# for an unserved tool name, for the same reason and with the same bound.
UNKNOWN_COMPONENT = "<unknown>"

# Every component name this process may publish, filled by `register_components` at the import of
# whichever module owns the name. A module-level set rather than a frozenset constant because the
# names belong to the servers and this package must not know them; what it owns is the *rule* that
# the set is closed before anything is counted. See `record`.
_COMPONENTS: set[str] = set()

DEGRADED = Counter(
    "chemclaw_mcp_degraded_total",
    "Answers returned with a component's contribution missing, by component and cause.",
    ("server", "component", "cause"),
)


def classify(exc: BaseException) -> str:
    """Which `CAUSES` member `exc` is, checked most specific first.

    `EgressForbidden` is tested before anything else deliberately: it is an `OSError`, so any
    branch that sorted `OSError` first would bury the one cause this fleet most needs to see. The
    errno branch below is exactly such a branch, which is why the order is load-bearing rather than
    tidy: a refusal carries `errno.EHOSTUNREACH`, not one of `_RESOURCE_ERRNOS`, so the two do not
    overlap today — and the ordering is what keeps that an accident the fleet does not depend on.

    **What stays `failed` on purpose, and is not an oversight.** A bare `TimeoutError` and an
    `asyncio.CancelledError` carry no errno, and a `RuntimeError("CUDA out of memory")` carries no
    type name this can match — matching its *message* is the control this module's docstring refuses
    to write, because torch has reworded it across releases. All three therefore read as permanent.
    That verdict is the cheap one to get wrong in this direction: since
    `D-2026-09-13-a-probe-that-can-kill-the-pod-is-not-a-readiness-probe` gave liveness its own
    route, an unready answer sheds traffic and is reversed by the next passing probe, where the
    opposite mistake leaves a broken pod serving.

    Args:
        exc: The exception a component raised.

    Returns:
        One of `CAUSES`.
    """
    if isinstance(exc, EgressForbidden):
        return CAUSE_EGRESS_REFUSED
    if type(exc).__name__ in _RESOURCE_TYPE_NAMES:
        return CAUSE_RESOURCE_EXHAUSTED
    if isinstance(exc, OSError) and exc.errno in _RESOURCE_ERRNOS:
        return CAUSE_RESOURCE_EXHAUSTED
    if isinstance(exc, ImportError):
        return CAUSE_NOT_INSTALLED
    return CAUSE_FAILED


def register_components(*names: str) -> None:
    """Declare component names this process may publish on `DEGRADED`.

    Called at the import of whichever module owns the name — `mapping.COMPONENT`, the predictor
    registry's own map — so the set is closed before the first `record`. Idempotent.

    **Why a registry and not a convention.** `cause` was clamped to `CAUSES` and `component` was
    clamped by nothing but the habit of spelling it as a module constant, so the two halves of one
    label rule were enforced by two different mechanisms and only one of them was a mechanism.
    Measured: `record(component='hostile"}\n fake_metric 99', ...)` minted that series, on an
    endpoint `CLAUDE.md` and `metrics.py` both describe as unauthenticated. No caller-reachable path
    reached it — every call site passes a source constant, and `rxnpredict`'s `_select` intersects a
    caller's model list with its registry before any of them — so this closes a gap rather than a
    breach, and it closes it the way `app._served_tool_name` closes the same gap for a tool name.

    Args:
        names: Component names, each a constant in the calling package's source.
    """
    _COMPONENTS.update(names)


def registered_components() -> frozenset[str]:
    """The component names declared so far, for a test that wants to bound the exposition."""
    return frozenset(_COMPONENTS)


def record(*, server: str, component: str, cause: str) -> None:
    """Count one degraded answer, clamping both labels rather than raising at either.

    **Lenient on purpose, and it is the `except` block around every call site that makes it so.**
    This function's three callers are all inside one — `mapping.map_reaction`, `naming.name`,
    `predictors.mark_unavailable` — whose whole job is to answer anyway. An earlier version raised
    `ValueError` for an unclamped cause, which `connector_app` passes to the model *verbatim*:
    driven with `classify` patched to return a fifth cause a later wave forgot to add to `CAUSES`,
    `map_reaction` raised instead of degrading and a chemist's answer became a sentence about
    Prometheus labels. The error reporter became the error. So an unrecognised cause is logged and
    counted as `CAUSE_FAILED`, an unregistered component as `UNKNOWN_COMPONENT`, and the hard
    assertion lives in the suite
    (`tests/test_degradation.py::test_every_cause_a_call_site_passes_is_one_of_the_four`), where a
    failure costs a red build rather than a chemist's answer.

    The clamp is still a clamp: a label outside the declared set never reaches `/metrics`, so the
    series count stays bounded by this file and by `register_components`' callers.

    Args:
        server: The `connector_app` name of the server reporting it.
        component: What went missing. A module-level constant at every call site, declared through
            `register_components`; anything else is collapsed onto `UNKNOWN_COMPONENT`.
        cause: A member of `CAUSES`; anything else is logged and counted as `CAUSE_FAILED`.
    """
    if cause not in CAUSES:
        logger.error(
            "%r is not a degradation cause; counted as %r instead, because `/metrics` is "
            "unauthenticated and this label is clamped to %s",
            cause,
            CAUSE_FAILED,
            sorted(CAUSES),
        )
        cause = CAUSE_FAILED
    if component not in _COMPONENTS:
        logger.error(
            "%r is not a registered degradation component; counted as %r instead. Declare it with "
            "`degradation.register_components` where the name is defined",
            component,
            UNKNOWN_COMPONENT,
        )
        component = UNKNOWN_COMPONENT
    DEGRADED.labels(server, component, cause).inc()
