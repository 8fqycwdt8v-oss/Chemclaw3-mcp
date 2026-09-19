"""When a component that would not build may be built again.

Two of this server's components are optional extras constructed once per process — `mapping`'s
atom mapper and `naming`'s namer — and both have the same problem: constructing one loads weights
or a rule table, so it can fail for a reason that will still be true in an hour (a truncated
checkpoint) or for one that will not (a busy minute, a file-descriptor ceiling). A pod that latches
on the second kind is a restart dressed as a readiness probe.

**This module exists because the rule was stated in one of them and asserted in the other.**
`mapping` carried `_ATTEMPTED_AT`, `_retry_due()` and a retry window; `naming`'s header claimed the
two modules were "symmetric with `mapping` deliberately" and `naming._TRIED` latched
unconditionally, so a `MemoryError` or an `EMFILE` while importing `rxn_insight` — which the shipped
image installs — took the namer out for the life of the process with nothing able to change its
mind. Driven before this: a transient failure on the first `_namer()` call left the second call
making **no second import attempt at all**, `_FAILURE` pinned to `resource_exhausted` and
`available()` false for ever. The classification was symmetric; the recovery was not.

So the predicate is written once, as a pure function of the two facts a caller already holds, and
each module keeps its own globals and its own lock. What is *not* shared is the decision to retry at
all: that is `mcp_server_kit.degradation.PERMANENT_CAUSES`, which is where this fleet already
decides which failures can improve.
"""

from __future__ import annotations

import time

from mcp_server_kit import degradation

__all__ = ["RETRY_SECONDS", "retry_due"]

#: How long a component whose construction failed transiently waits before it is tried again. One
#: minute: long enough that a pod under memory pressure is not re-attempting a weight load in a
#: tight loop, short enough that a readiness probe on its default interval lifts within a few
#: cycles.
RETRY_SECONDS = 60.0


def retry_due(*, failure: str | None, attempted_at: float | None, window_seconds: float) -> bool:
    """Whether a construction that failed may be attempted again now.

    Args:
        failure: The cause the last attempt failed with, as `degradation.classify` named it, or
            `None` when nothing has failed — which includes the extra simply not being installed,
            and that is deliberately not a retry: re-importing an absent distribution every minute
            learns nothing.
        attempted_at: `time.monotonic()` at the last attempt, or `None` if none has run.
        window_seconds: The caller's own retry window, passed in rather than read from
            `RETRY_SECONDS` so a test can shorten one module's window without touching the other's.

    Returns:
        True only for a cause that can improve, and only once the window has elapsed.
    """
    if failure is None or failure in degradation.PERMANENT_CAUSES:
        return False
    return attempted_at is not None and (time.monotonic() - attempted_at >= window_seconds)
