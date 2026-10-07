"""Counters for this server's cost control: budget kills and inline-budget stops.

They complement the fleet's tool-call metrics, which cannot tell a run killed at its timeout from
a 3 ms parse failure. Subprocess metrics are labelled by binary name (`xtb`, `crest`), bounded by
the image; `INLINE_BUDGET_EXCEEDED` by the literal `what` `Deadline.check` is called with
(Hessian vs geometry optimisation). `/metrics` is unauthenticated, so no actor, session,
correlation id or argument is ever a label.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

__all__ = [
    "INLINE_BUDGET_EXCEEDED",
    "PROCESS_GROUP_KILLS",
    "SUBPROCESS_DURATION",
    "SUBPROCESS_TIMEOUTS",
]

# Shaped like the fleet histogram, up to the CREST budget, so real runs never land in `+Inf`.
_BUCKETS = (0.1, 1.0, 5.0, 15.0, 60.0, 300.0, 900.0, 3600.0, 14400.0)

SUBPROCESS_DURATION = Histogram(
    "chemclaw_mcp_calc_subprocess_duration_seconds",
    "Wall-clock seconds one calculation subprocess ran, whether it finished or was killed.",
    ("binary",),
    buckets=_BUCKETS,
)

SUBPROCESS_TIMEOUTS = Counter(
    "chemclaw_mcp_calc_subprocess_timeouts_total",
    "Calculation subprocesses that exceeded their wall-clock budget.",
    ("binary",),
)

PROCESS_GROUP_KILLS = Counter(
    "chemclaw_mcp_calc_process_group_kills_total",
    "Process groups SIGKILLed after a timeout, including whatever the run had forked.",
    ("binary",),
)

INLINE_BUDGET_EXCEEDED = Counter(
    "chemclaw_mcp_calc_inline_budget_exceeded_total",
    "In-process calculations stopped because their inline wall-clock budget was spent.",
    ("what",),
)
