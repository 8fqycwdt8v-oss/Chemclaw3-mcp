"""A degraded answer, counted and classified.

Wherever a broken component still yields an answer, the cause is counted on
`chemclaw_mcp_degraded_total{server,component,cause}` so a degraded pod is visible from a scrape.

- `EgressForbidden` is classified first: it subclasses `OSError`, so a library's own retry could
  otherwise bury a no-egress refusal inside a plausible answer.
- Both labels are closed sets (`CAUSES`, `register_components`), because `/metrics` is
  unauthenticated and unbounded labels are not allowed.
- `classify` reads exception types and errnos, never messages, and the resource branch stays
  narrow: a cause wrongly marked transient leaves a broken pod in service.
"""

from __future__ import annotations

import errno
import logging
from collections.abc import Collection

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
    "is_not_installed",
    "record",
    "register_components",
    "registered_components",
]

# The in-process egress guard refused an outbound call this component needed. Permanent: the
# deployment's posture does not change under load.
CAUSE_EGRESS_REFUSED = "egress_refused"

# The process ran out of something it can get back: memory, a device allocation. **The one cause
# that is transient**, and the one a readiness check must not act on — see `PERMANENT_CAUSES`.
CAUSE_RESOURCE_EXHAUSTED = "resource_exhausted"

# The component's distribution is not in this image — a deployment's decision, not a fault. Only a
# `ModuleNotFoundError` earns it; see `is_not_installed`.
CAUSE_NOT_INSTALLED = "not_installed"

# Everything else: an unparseable checkpoint, a corrupt table, a library raising on an input.
CAUSE_FAILED = "failed"

CAUSES = frozenset(
    {CAUSE_EGRESS_REFUSED, CAUSE_RESOURCE_EXHAUSTED, CAUSE_NOT_INSTALLED, CAUSE_FAILED}
)

# The causes meaning *this pod will keep answering wrongly until replaced*. Only these may make a
# readiness check unready (enforced by `connector_app`'s `/healthz`): a transient failure says more
# about a heavy probe than about the calls, and an absent optional component is by design.
PERMANENT_CAUSES = frozenset({CAUSE_EGRESS_REFUSED, CAUSE_FAILED})

# Exception type names meaning a resource ran out: CPython's `MemoryError`, and torch's
# `OutOfMemoryError` (a `RuntimeError` subclass), matched by name so this package need not import
# torch.
_RESOURCE_TYPE_NAMES = frozenset({"MemoryError", "OutOfMemoryError"})

# `OSError.errno` values meaning the kernel refused a recoverable resource: memory, file
# descriptors (`EMFILE`/`ENFILE`), threads/processes (`EAGAIN`, which is `EWOULDBLOCK` on Linux).
_RESOURCE_ERRNOS = frozenset({errno.ENOMEM, errno.EMFILE, errno.ENFILE, errno.EAGAIN})

# The component label a name nothing registered collapses onto. The sentinel spelling `app.py` uses
# for an unserved tool name, for the same reason and with the same bound.
UNKNOWN_COMPONENT = "<unknown>"

# Every component name this process may publish, filled by `register_components` at import of the
# owning module; the set is closed before anything is counted.
_COMPONENTS: set[str] = set()

DEGRADED = Counter(
    "chemclaw_mcp_degraded_total",
    "Answers returned with a component's contribution missing, by component and cause.",
    ("server", "component", "cause"),
)


def classify(exc: BaseException, *, optional: Collection[str] | None = None) -> str:
    """Which `CAUSES` member `exc` is, checked most specific first.

    `EgressForbidden` is tested first because it is an `OSError` and the errno branch would
    otherwise
    see it. `not_installed` is a `ModuleNotFoundError` only (see `is_not_installed`); a plain
    `ImportError` is a broken image. A bare `TimeoutError`, a cancellation and a CUDA OOM reported
    only
    by message stay `failed` — messages are never matched.

    Args:
        exc: The exception a component raised.
        optional: The top-level modules whose absence is a deployment's decision, or `None` where
            the caller cannot say.

    Returns:
        One of `CAUSES`.
    """
    if isinstance(exc, EgressForbidden):
        return CAUSE_EGRESS_REFUSED
    if type(exc).__name__ in _RESOURCE_TYPE_NAMES:
        return CAUSE_RESOURCE_EXHAUSTED
    if isinstance(exc, OSError) and exc.errno in _RESOURCE_ERRNOS:
        return CAUSE_RESOURCE_EXHAUSTED
    if isinstance(exc, ModuleNotFoundError) and (
        optional is None or is_not_installed(exc, optional)
    ):
        return CAUSE_NOT_INSTALLED
    return CAUSE_FAILED


def is_not_installed(exc: BaseException, optional: Collection[str]) -> bool:
    """Whether `exc` says one of the `optional` top-level modules is simply absent from this image.

    Only a `ModuleNotFoundError` whose `name` is exactly one of `optional` qualifies. A plain
    `ImportError` (found but would not load), a missing dependency of an installed extra, a missing
    submodule (`name="a.b"`), or an error without `name` all mean a broken image.

    Args:
        exc: What the guarded import raised.
        optional: The top-level module names whose absence is a deployment's decision.

    Returns:
        `True` only for a `ModuleNotFoundError` naming one of `optional` itself.
    """
    if not isinstance(exc, ModuleNotFoundError) or not exc.name:
        return False
    return exc.name in optional


def register_components(*names: str) -> None:
    """Declare component names this process may publish on `DEGRADED`.

    Called at import of the owning module, so the label set is closed before the first `record`;
    an unregistered name is clamped rather than minted as a series. Idempotent.

    Args:
        names: Component names, each a constant in the calling package's source.
    """
    _COMPONENTS.update(names)


def registered_components() -> frozenset[str]:
    """The component names declared so far, for a test that wants to bound the exposition."""
    return frozenset(_COMPONENTS)


def record(*, server: str, component: str, cause: str) -> None:
    """Count one degraded answer, clamping both labels rather than raising at either.

    Every caller is inside an `except` whose job is to answer anyway, so raising here would turn a
    label problem into the model's answer. An unknown cause is logged and counted as `CAUSE_FAILED`,
    an unregistered component as `UNKNOWN_COMPONENT`; the strict check lives in the test suite.

    Args:
        server: The `connector_app` name of the server reporting it.
        component: What went missing, declared through `register_components`.
        cause: A member of `CAUSES`; anything else is counted as `CAUSE_FAILED`.
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
