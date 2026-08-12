"""The `X-Chemclaw-*` caller headers: recorded on every request, trusted for nothing.

Chemclaw3 stamps four headers on every connector call (`chemclaw.connectors.identity`). They exist
so a server's own log lines and records can be joined to the core audit trail by actor and session
— which is a GxP requirement, not a convenience.

**They are advisory in the strict sense.** Authorization happened in Chemclaw3 before the call was
made; a header on a request is not evidence of anything a server here should act on. A server that
gated a tool on `X-Chemclaw-Actor` would be trusting an unauthenticated string, and would do so
while looking like it had access control. The credential on the wire is the bearer token
(`auth.py`); the headers are provenance.

The contextvars are set twice on purpose — once per HTTP request, and again per MCP tool call.
Chemclaw3 measured why: an MCP tool body does not run in the ASGI task, it runs in the session
manager's, so a value bound in middleware is whatever the *handshake* carried. Over one session,
a handshake as alice followed by a `tools/call` as bob had the tool body reading alice. Anything a
tool stamps must come from the request that tool call is serving.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass

HEADER_ACTOR = "x-chemclaw-actor"
HEADER_SESSION = "x-chemclaw-session"
HEADER_CORRELATION = "x-chemclaw-correlation"
HEADER_DRY_RUN = "x-chemclaw-dry-run"

_actor: ContextVar[str] = ContextVar("chemclaw_actor", default="")
_session: ContextVar[str] = ContextVar("chemclaw_session", default="")
_correlation: ContextVar[str] = ContextVar("chemclaw_correlation", default="")


@dataclass(frozen=True, slots=True)
class Caller:
    """Who Chemclaw3 says is asking. Every field may be empty; none of them grants anything."""

    actor: str = ""
    session: str = ""
    correlation: str = ""


CallerTokens = tuple[Token[str], Token[str], Token[str]]


def bind_caller(actor: str, session: str, correlation: str) -> CallerTokens:
    """Bind the caller for the current context, returning the tokens `reset_caller` needs."""
    return (_actor.set(actor), _session.set(session), _correlation.set(correlation))


def reset_caller(tokens: CallerTokens) -> None:
    """Undo `bind_caller`. Always in a `finally`, so one failed call cannot mislabel the next."""
    actor_token, session_token, correlation_token = tokens
    _actor.reset(actor_token)
    _session.reset(session_token)
    _correlation.reset(correlation_token)


def current_caller() -> Caller:
    """The caller of the tool call running now — for logs and records, never for a decision."""
    return Caller(actor=_actor.get(), session=_session.get(), correlation=_correlation.get())
