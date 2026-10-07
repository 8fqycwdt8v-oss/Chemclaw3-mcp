"""Continue the trace Chemclaw3 already sends, so a tool call is a span inside the turn that asked.

Extracts the incoming W3C `traceparent`, attaches it and opens a span under it. Constraints:

- **Inert by default and never an outbound connection**: no exporter, provider or processor is
  constructed; without a configured SDK the tracer is a non-recording proxy. Exporting needs a
  deployment to install an SDK and open a destination, argued like `MCP_EGRESS_ALLOW`.
- **Off unless `MCP_TRACING_ENABLED`**, and `opentelemetry-api` is an optional `otel` extra;
  absent, the span is skipped, never raised.
- **Identifiers only**: server and (clamped) tool name, never arguments, results or the caller.
  Exception recording and status descriptions are off, since both would carry unredacted text;
  an outcome is a bare `StatusCode.ERROR`, decided by the caller's `is_refusal` so the span agrees
  with the metric.

A forged `traceparent` can only misplace spans; it buys no authority.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager

logger = logging.getLogger(__name__)

__all__ = ["TRACEPARENT", "TRACER_NAME", "TRACING_ENABLED_ENV", "tool_call_span", "tracing_enabled"]

# The W3C trace-context header, named so a reader sees what this server consumes.
TRACEPARENT = "traceparent"

# Off unless a deployment says otherwise. Same spelling as `MCP_EGRESS_GUARD`, opposite default.
TRACING_ENABLED_ENV = "MCP_TRACING_ENABLED"

# The instrumentation scope every span from this kit is created under, so a collector can separate
# "spans this fleet wrote" from the ones a deployment's auto-instrumentation produces.
TRACER_NAME = "mcp_server_kit"

_TRUE = frozenset({"1", "true", "yes", "on"})


def tracing_enabled() -> bool:
    """Whether `MCP_TRACING_ENABLED` is set to something affirmative. Off by default.

    Read at call time, so it can change without reloading the module.
    """
    return os.environ.get(TRACING_ENABLED_ENV, "").strip().lower() in _TRUE


@contextmanager
def tool_call_span(
    headers: Mapping[str, str],
    *,
    server: str,
    tool: str,
    is_refusal: Callable[[BaseException], bool],
) -> Iterator[None]:
    """Run one tool call inside a span, parented on the incoming `traceparent` when there is one.

    `tool` is already clamped by the caller; `is_refusal` decides whether a propagating exception
    marks the span an error, and nothing else about the exception reaches it. Runs the block
    unchanged when tracing is off, the API is absent, or no context could be extracted.
    """
    if not tracing_enabled():
        yield
        return
    try:
        from opentelemetry import context as otel_context
        from opentelemetry import trace
        from opentelemetry.propagate import extract
        from opentelemetry.trace import Status, StatusCode
    except ImportError:
        # The `otel` extra is not installed; debug, so a server not using tracing stays quiet.
        logger.debug("tracing is enabled but opentelemetry-api is not installed")
        yield
        return

    # Attached even with no `traceparent` present: `extract` then yields an empty context, the span
    # below becomes a root, and the code path is the one that runs in production either way.
    token = otel_context.attach(extract(dict(headers)))
    try:
        tracer = trace.get_tracer(TRACER_NAME)
        # Both recording defaults off: they write exception text into the span, bypassing redaction.
        with tracer.start_as_current_span(
            f"mcp.tool/{tool}", record_exception=False, set_status_on_exception=False
        ) as span:
            span.set_attribute("mcp.server", server)
            span.set_attribute("mcp.tool", tool)
            try:
                yield
            except BaseException as exc:
                # A bare enum value and no description. An ERROR span says "this server failed the
                # call"; what failed is in the log line the `error_id` joins it to.
                if not is_refusal(exc):
                    span.set_status(Status(StatusCode.ERROR))
                raise
    finally:
        otel_context.detach(token)
