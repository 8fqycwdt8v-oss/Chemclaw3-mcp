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

# The four names Chemclaw3 stamps, spelled exactly as `chemclaw.connectors.identity` writes them.
# Lower-cased here only because that is how this repository reads them; HTTP header lookup is
# case-insensitive, so the case is free and the *rest of the string* is not.
#
# `HEADER_CORRELATION` used to read `x-chemclaw-correlation`, against a sender that writes
# `X-Chemclaw-Correlation-Id`. Header lookup is case-insensitive, not suffix-insensitive, so
# `headers.get(...)` returned `None` and every server in this fleet bound `correlation=""` — on
# every request, since the header was introduced. Measured against a running `connector_app`: with
# the sender's spelling a tool body read `""`, with this file's it read the value. Nothing here
# consumed `current_caller().correlation` yet, which is the only reason it was invisible; the first
# server to stamp a record with it would have written an empty string into the one field that joins
# this fleet's records to Chemclaw3's audit trail. `tests/test_identity_contract.py` is what stops
# the next rename, and it asserts the *sent* spellings rather than these constants.
HEADER_ACTOR = "x-chemclaw-actor"
HEADER_SESSION = "x-chemclaw-session"
HEADER_CORRELATION = "x-chemclaw-correlation-id"
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
