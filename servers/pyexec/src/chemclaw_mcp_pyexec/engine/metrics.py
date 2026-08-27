"""How the sandbox's runs ended — the one thing this server had no record of at all.

`servers/pyexec/src` contained **zero** log statements and published no application metric, while
the sandbox is the piece of this fleet with the most ways to end badly: a program can finish, raise,
run out of wall clock and have its process group killed, or be destroyed by a resource limit
(SIGXCPU, `RLIMIT_AS`, the OOM killer) without writing a result at all. All four looked identical
from outside the pod, and the last two — the ones that mean a bound is being hit — were the two the
caller could not distinguish either.

`outcome` is a closed set of four literals, so the label is bounded by this file rather than by
anything a caller sends. Nothing here takes the program, its output or its caller as a label:
`/metrics` is unauthenticated, and a submitted program is the most argument-shaped thing in the
fleet.
"""

from __future__ import annotations

from prometheus_client import Counter

__all__ = ["RUNS"]

RUNS = Counter(
    "chemclaw_mcp_pyexec_runs_total",
    "Sandboxed analysis runs, by how they ended: ok, error, timeout or killed.",
    ("outcome",),
)
