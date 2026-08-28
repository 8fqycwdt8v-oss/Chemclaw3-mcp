"""What this server's cost control actually did — the counters `/metrics` had none of.

`servers/calc` is where this fleet's money and hours go: `xtb_cli_timeout_seconds` is 3600 and
`crest_timeout_seconds` is 14400, and before this the whole server carried three log statements and
no application metric at all. So the two events that matter most about a calculation pod — *it
killed a run for going over budget*, and *it refused a run before starting because the budget was
already spent* — were invisible to a scrape and, in the second case, visible **only to the model**:
`Deadline.check` raises a `ValueError`, which `connector_app` passes to the caller verbatim and
therefore never logs.

These are the fleet-wide metrics' complement, not a duplicate of them.
`chemclaw_mcp_tool_calls_total{outcome="failed"}` says a call did not answer; it cannot say whether
a process group was killed at 3600 s or a SMILES failed to parse in 3 ms, and those need different
responses — the first is an undersized pod or an oversized molecule, the second is a caller error.

Two label sets, and both are bounded by something the image or this file fixes rather than by
anything a caller sends. The subprocess metrics are labelled by **binary name** (`xtb`, `crest`),
bounded by what the image installs. `INLINE_BUDGET_EXCEEDED` is labelled by **what** ran out of
budget, which is a bounded literal `Deadline.check` is called with — and it carried no label at
all until now, so an operator could see that an inline budget was spent and not whether it was a
Hessian's or a geometry optimisation's, which are different decisions: a Hessian scales with the
molecule and an optimisation with the surface. (The header here said "every label is a binary
name", which was false in the direction that hid the gap.)

`/metrics` is unauthenticated, so the rule `mcp_server_kit/metrics.py` states applies unchanged: no
actor, no session, no correlation id, and no argument — a molecule is an argument, and `what` is
not one.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

__all__ = [
    "INLINE_BUDGET_EXCEEDED",
    "PROCESS_GROUP_KILLS",
    "SUBPROCESS_DURATION",
    "SUBPROCESS_TIMEOUTS",
]

# The same shape as the fleet histogram's, because the questions are asked together: a CREST search
# is hours by design, and a bucket set that topped out at a minute would put every real run in
# `+Inf` and answer nothing.
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
