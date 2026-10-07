"""The `X-Chemclaw-*` caller headers: recorded on every request, trusted for nothing.

Chemclaw3 stamps them so a server's logs and records join the core audit trail. They are
provenance only: authorization happened in Chemclaw3, the credential is the bearer token
(`auth.py`), and a server must never gate on a header.

The contextvars are set per HTTP request and again per tool call, because a tool body runs in the
session manager's task and would otherwise read the handshake's caller.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass

# The names Chemclaw3 sends, lower-cased (lookup is case-insensitive, nothing else is).
# `tests/test_identity_contract.py` holds them against the sender's spellings.
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
