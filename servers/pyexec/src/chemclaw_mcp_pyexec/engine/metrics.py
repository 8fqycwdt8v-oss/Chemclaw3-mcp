"""How the sandbox's runs ended: ok, error, timeout, or killed by a resource limit.

`outcome` is a closed set of literals, so the label is bounded by this file. Nothing here takes the
program, its output or its caller as a label: `/metrics` is unauthenticated.
"""

from __future__ import annotations

from prometheus_client import Counter

__all__ = ["RUNS"]

RUNS = Counter(
    "chemclaw_mcp_pyexec_runs_total",
    "Sandboxed analysis runs, by how they ended: ok, error, timeout or killed.",
    ("outcome",),
)
