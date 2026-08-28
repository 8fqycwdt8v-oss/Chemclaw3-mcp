"""Continue the trace Chemclaw3 already sends, so a tool call is a span inside the turn that asked.

Chemclaw3 stamps W3C trace context on every connector call (`chemclaw.core.tracing.trace_headers`,
injected by `connectors/identity.py`) and its own docstring says the consequence out loud: "a
connector's work appears inside the turn that asked for it instead of as an orphan trace an operator
has to know to go looking for". That held for the connectors Chemclaw3 hosts in its own process. It
did not hold for this fleet: every server here read the four `X-Chemclaw-*` headers and dropped
`traceparent` on the floor, so the expensive half of a chemist's question — a CREST search, a
Hessian — produced **no span at all** on this side. Correlation-id log joining still worked, which
is why the gap read as fine rather than as broken, and it is the weaker half of the pair: a
correlation id joins log lines after the fact, by grep, and trace context joins spans live.

This module is the receiving half, and it is the same shape as the sending one because it has to
be: `extract` the incoming carrier, `attach` it, open a span under it.

**Three constraints shape it, and together they are why this is one small module rather than an
instrumentation package.**

- **Inert by default, and never an outbound connection.** The egress guard is armed on import in
  every one of these processes and the whole suite runs with it armed, so an exporter that dials a
  collector would raise `EgressForbidden` — correctly. Nothing here constructs an exporter, a
  provider or a batch processor. It asks `opentelemetry.trace` for a tracer, which without a
  configured SDK is a proxy that produces non-recording spans and no I/O of any kind. A deployment
  that wants the spans exported installs and configures the SDK itself (`opentelemetry-instrument`,
  or its own bootstrap) and sets a destination the NetworkPolicy allows — which is a decision to
  argue for, exactly like `MCP_EGRESS_ALLOW`.
- **Off unless a deployment says otherwise.** `MCP_TRACING_ENABLED` gates it, defaulting to off, in
  the spelling `MCP_EGRESS_GUARD` already uses. Two gates rather than one: with the SDK absent there
  is nothing to export, and with the SDK present-but-unwanted there is still nothing, because the
  variable decides. The cost on the ordinary path is one environment read.
- **Optional, not a fleet-wide dependency.** `opentelemetry-api` is the kit's `otel` extra rather
  than a runtime dependency, because the reason `props` starts in under a second is that it carries
  nothing it does not need. Absent, the import fails and the span is skipped — logged at debug, not
  raised, because tracing must never break a tool call.

**What a span may carry is the rule `/metrics` follows, for the same reason.** A span attribute
travels to a collector, so this writes identifiers only — the server's name and the tool's — and
never an argument, a result or a caller. The `X-Chemclaw-*` identity headers are deliberately *not*
attached: they are provenance for this server's own logs, and putting an actor on a span would
publish per-actor call volumes to whatever the collector shows.

**That sentence was false for as long as an exception could reach the span, and the leak was the
one channel bypassing every other control.** `start_as_current_span` defaults to
`record_exception=True` and `set_status_on_exception=True`, so a propagating exception put
`format_exception`'s whole rendered chain into an `exception.stacktrace` attribute and `str(exc)`
into the status description — neither of which passes through `_sanitize_tool_errors` (which
replaces the *message* but keeps the cause: `raise ToolError(...) from exc.__cause__`) or through
`SecretRedactingFilter` (which sees log records, not spans). Measured against a running server with
an in-memory SDK exporter: a tool raising `RuntimeError("PGPASSWORD=hunter2 ... bearer=abc123...")`
produced a span carrying **`hunter2`** and the bearer in clear, while the log line for the same
fault read `PGPASSWORD=***`; a `ValueError` naming a caller's molecule put that molecule in
`exception.message`. Both defaults are therefore off, and the only thing an outcome writes is a
bare `StatusCode.ERROR` — a status is one enum value and cannot carry text.

**And the outcome the status records is the metric's, not the transport's.** Every `ToolError` that
propagates is an exception, so recording "did something raise" made a *refusal* — the answer
`chemclaw_mcp_tool_calls_total` books as `outcome="refused"`, and the whole content of a correct
answer — an ERROR span, while the counter for the same call said the server was fine. The caller
passes `is_refusal`, which is `app._is_caller_safe`: one discriminator, read by the sanitiser, the
counter and the span alike, because two spellings of it is how an operator's trace view comes to
disagree with the dashboard beside it about the same call.

**Trace context is safe to trust from outside, unlike those headers**, and the difference is worth
stating where both arrive on the same request. Authorization happened in Chemclaw3; a forged
`X-Chemclaw-Actor` would be an unauthenticated string reaching a decision. The worst a forged
`traceparent` can do is attach spans to a trace that is not theirs. It buys no authority.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager

logger = logging.getLogger(__name__)

__all__ = ["TRACEPARENT", "TRACER_NAME", "TRACING_ENABLED_ENV", "tool_call_span", "tracing_enabled"]

# The standard W3C trace-context header, named here rather than reached for through the propagator
# so a reader can see what this server consumes without following an indirection into OTel. The
# sender is `chemclaw.core.tracing.TRACEPARENT`, and the spelling is the standard's, lower-case.
TRACEPARENT = "traceparent"

# Off unless a deployment says otherwise. Same spelling as `MCP_EGRESS_GUARD`, opposite default.
TRACING_ENABLED_ENV = "MCP_TRACING_ENABLED"

# The instrumentation scope every span from this kit is created under, so a collector can separate
# "spans this fleet wrote" from the ones a deployment's auto-instrumentation produces.
TRACER_NAME = "mcp_server_kit"

_TRUE = frozenset({"1", "true", "yes", "on"})


def tracing_enabled() -> bool:
    """Whether `MCP_TRACING_ENABLED` is set to something affirmative. Off by default.

    Read at call time rather than at import, so a test — and an operator with a rolling restart —
    can change it without reloading the module.
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

    Args:
        headers: The serving request's headers. Only `traceparent`/`tracestate` are read, and only
            by the propagator; nothing else on the request reaches the span.
        server: The server's manifest name — the same string the directory, the package suffix and
            `CHEMCLAW_CONNECTOR_URLS` use.
        tool: The tool being called, already clamped to the served surface by the caller. A span
            name is as caller-supplied as a metric label and is folded the same way: unclamped,
            this recorded `mcp.tool/../../etc/passwd?a=b` and a 318-character name.
        is_refusal: Whether a propagating exception is a refusal the caller asked for rather than a
            fault of this server's. The caller's own discriminator, so the span's outcome and the
            counter's cannot drift apart. Nothing about the exception itself reaches the span.

    Yields:
        Nothing. The block runs inside the span, or unchanged when tracing is off, the API is not
        installed, or the context could not be extracted.
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
        # The `otel` extra is not installed. Debug rather than a warning: a server that never
        # enabled tracing on purpose should not log on every call, and one that did will find the
        # line the first time it looks.
        logger.debug("tracing is enabled but opentelemetry-api is not installed")
        yield
        return

    # Attached even with no `traceparent` present: `extract` then yields an empty context, the span
    # below becomes a root, and the code path is the one that runs in production either way.
    token = otel_context.attach(extract(dict(headers)))
    try:
        tracer = trace.get_tracer(TRACER_NAME)
        # Both recording defaults off: `record_exception` writes the rendered exception chain into
        # a span attribute, and `set_status_on_exception` writes `str(exc)` into the status
        # description. Neither passes the sanitiser or the log redaction, so both are leaks by
        # construction rather than by accident — see the module docstring.
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
