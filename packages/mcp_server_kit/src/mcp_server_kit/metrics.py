"""The application metrics `/metrics` publishes — because until this file existed it published none.

Measured before this: **ten** series on a running server, all ten `prometheus_client`'s built-in
`python_*` and `process_*` collectors. So an operator could see the interpreter's version and the
pod's open file descriptors, and could not answer *which tool is slow*, *which tool is failing*, or
*is anything being called at all* — on a fleet whose flagship server runs CREST searches with a
14,400 s budget.

**What may and may not be a label here is the whole design, because this endpoint is
unauthenticated.** `CLAUDE.md`'s rule is that no actor, no session, no correlation id and no tool
*argument* may ever be a label: those would publish per-caller behaviour to anything that can reach
the pod. A tool *name* is none of the four, and it is bounded by the registered surface — the same
judgement Chemclaw3 makes for `chemclaw_repeated_tool_calls_total{tool}`.

**Bounded only if it is clamped, and it is not bounded by construction.** The name in a
`tools/call` is caller-supplied: `ToolManager.call_tool` raises `Unknown tool: <whatever>` for
anything it does not have, so an unclamped counter mints a series per string a confused model or a
hostile caller sends. Measured in the audit's prototype: a probe calling `nope` minted
`tool="nope"`. `app.py` therefore resolves every name against the manager's own registry and folds
anything else into `UNKNOWN_TOOL`, and `tests/test_metrics.py` drives a real unknown-tool call to
prove it.

The same rule is why `chemclaw_mcp_egress_refused_total` carries **no** label at all: the
destination host of a refused connection is attacker-influenced and unbounded, and `rate(...) > 0`
is the whole alert.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

__all__ = [
    "BUILD_INFO",
    "EGRESS_GUARD_ARMED",
    "EGRESS_REFUSED",
    "READY",
    "REQUESTS",
    "TOOL_CALLS",
    "TOOL_DURATION",
    "UNAUTHENTICATED_REQUESTS",
    "UNKNOWN_TOOL",
]

# What an unrecognised tool name is counted as. A fixed sentinel rather than the string the caller
# sent, so the label set stays bounded by the served surface.
UNKNOWN_TOOL = "<unknown>"

# Spanning the fleet's real range in one bucket set: `props` answers a vapour pressure in
# microseconds, and `crest_timeout_seconds` is 14,280 — the longest legal call on this fleet,
# held 120 s under its caller's own budget so the actionable refusal is the one that wins.
# The top bucket stays a round 14400 deliberately: a bucket edge is a reporting boundary
# rather than a claim about a timeout, and moving it with every budget change would
# discard the histogram's history for nothing. A shared histogram is right because the
# question — "which tool is slow" — is asked across servers, and a per-server bucket set would make
# two servers' p95 incomparable.
DURATION_BUCKETS = (0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 15.0, 60.0, 300.0, 900.0, 3600.0, 14400.0)

TOOL_CALLS = Counter(
    "chemclaw_mcp_tool_calls_total",
    "MCP tool calls, by served tool and outcome.",
    ("server", "tool", "outcome"),
)

TOOL_DURATION = Histogram(
    "chemclaw_mcp_tool_duration_seconds",
    "Wall-clock seconds one MCP tool call took, measured around the tool manager.",
    ("server", "tool"),
    buckets=DURATION_BUCKETS,
)

REQUESTS = Counter(
    "chemclaw_mcp_requests_total",
    "HTTP requests served, by route and response status.",
    ("server", "path", "status"),
)

UNAUTHENTICATED_REQUESTS = Counter(
    "chemclaw_mcp_unauthenticated_requests_total",
    "Requests refused because they carried no valid credential.",
    ("server",),
)

BUILD_INFO = Gauge(
    "chemclaw_mcp_build_info",
    "Always 1; the labels name the build this process is.",
    ("server", "revision"),
)

READY = Gauge(
    "chemclaw_mcp_ready",
    "1 when this server's readiness check last passed, 0 when it last failed.",
    ("server",),
)

EGRESS_REFUSED = Counter(
    "chemclaw_mcp_egress_refused_total",
    "Outbound connections the runtime guard refused. Deliberately unlabelled.",
)

EGRESS_GUARD_ARMED = Gauge(
    "chemclaw_mcp_egress_guard_armed",
    "1 when the in-process egress guard is installed, 0 when it is not.",
)
