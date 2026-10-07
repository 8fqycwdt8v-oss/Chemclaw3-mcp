"""The application metrics `/metrics` publishes.

`/metrics` is unauthenticated, so no actor, session, correlation id or tool argument may ever be a
label. A tool name may, clamped by `app.py` to the served surface (anything else folds into
`UNKNOWN_TOOL`), because the name in a `tools/call` is caller-supplied. A refused egress
destination is unbounded, so `chemclaw_mcp_egress_refused_total` has no labels.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

__all__ = [
    "ADMISSION_CEILING",
    "ADMISSION_IN_FLIGHT",
    "ADMISSION_REFUSED",
    "BUILD_INFO",
    "EGRESS_ALLOWED_HOSTS",
    "EGRESS_GUARD_ARMED",
    "EGRESS_REFUSED",
    "READY",
    "REQUESTS",
    "SESSIONS_CEILING",
    "SESSIONS_LIVE",
    "SESSIONS_REFUSED",
    "TOOL_CALLS",
    "TOOL_DURATION",
    "UNAUTHENTICATED_REQUESTS",
    "UNKNOWN_TOOL",
]

# What an unrecognised tool name is counted as. A fixed sentinel rather than the string the caller
# sent, so the label set stays bounded by the served surface.
UNKNOWN_TOOL = "<unknown>"

# One bucket set spanning the fleet (microseconds to the longest CREST budget), so p95s compare
# across servers. Edges are reporting boundaries, not timeouts, and stay fixed to keep history.
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

# Session occupancy, ceiling and refusals, labelled by server only — a count names no caller.
SESSIONS_LIVE = Gauge(
    "chemclaw_mcp_sessions_live",
    "MCP sessions this process is currently holding open.",
    ("server",),
)

SESSIONS_CEILING = Gauge(
    "chemclaw_mcp_sessions_ceiling",
    "The configured ceiling on concurrent MCP sessions. Absent when a deployment turns it off.",
    ("server",),
)

SESSIONS_REFUSED = Counter(
    "chemclaw_mcp_sessions_refused_total",
    "Session handshakes refused because the pod was already holding its ceiling.",
    ("server",),
)

# Admission occupancy: the autoscaling signal, since a full pod need not look busy by CPU.
# `in_flight / ceiling` is "how full" in the gate's own unit. Labelled by server only.
ADMISSION_IN_FLIGHT = Gauge(
    "chemclaw_mcp_admission_in_flight",
    "Admission slots held right now by admitted work.",
    ("server",),
)

ADMISSION_CEILING = Gauge(
    "chemclaw_mcp_admission_ceiling",
    "The configured admission ceiling, in slots.",
    ("server",),
)

ADMISSION_REFUSED = Counter(
    "chemclaw_mcp_admission_refused_total",
    "Calls refused at admission because the pod's slots were all held.",
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

# The number of allowed egress hosts, never the hosts themselves; shipped value is 0.
EGRESS_ALLOWED_HOSTS = Gauge(
    "chemclaw_mcp_egress_allowed_hosts",
    "How many hosts beyond loopback the egress guard is configured to permit. 0 when shipped.",
)
