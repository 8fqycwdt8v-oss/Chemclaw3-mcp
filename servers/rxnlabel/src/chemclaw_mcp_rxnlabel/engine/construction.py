"""When a component that would not build may be built again.

`mapping`'s mapper and `naming`'s namer are optional components constructed once per process.
Construction can fail permanently (a truncated checkpoint) or transiently (memory pressure, a
descriptor ceiling); latching on a transient failure would need a restart to recover. The retry
predicate is written once here; which causes are permanent is
`mcp_server_kit.degradation.PERMANENT_CAUSES`. Each module keeps its own state and lock.
"""

from __future__ import annotations

import time

from mcp_server_kit import degradation

__all__ = ["RETRY_SECONDS", "retry_due"]

#: Seconds a transiently failed construction waits before it is retried: no tight reload loop under
#: memory pressure, yet readiness recovers within a few probe cycles.
RETRY_SECONDS = 60.0


def retry_due(*, failure: str | None, attempted_at: float | None, window_seconds: float) -> bool:
    """Whether a construction that failed may be attempted again now.

    Args:
        failure: The cause the last attempt failed with, as `degradation.classify` named it, or
            `None` when nothing failed (including the extra not being installed, which is never
            retried).
        attempted_at: `time.monotonic()` at the last attempt, or `None` if none has run.
        window_seconds: The caller's own retry window.

    Returns:
        True only for a cause that can improve, and only once the window has elapsed.
    """
    if failure is None or failure in degradation.PERMANENT_CAUSES:
        return False
    return attempted_at is not None and (time.monotonic() - attempted_at >= window_seconds)
