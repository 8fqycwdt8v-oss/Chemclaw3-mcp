"""What a log line in this fleet carries — and who was deciding that before `logging.py` existed.

The finding this file pins is an *ordering* fact about somebody else's library, which is why it is
written as a measurement rather than as a source read. Nothing in `packages/` or `servers/` called
`basicConfig`, `dictConfig` or read a log level from the environment; lines appeared anyway,
because `FastMCP.__init__` calls `configure_logging(...)` on its way up. That is upstream's
prerogative — but it meant this fleet's log format, level and destination were an undeclared side
effect of a constructor, and `rich` is absent from every image here, so the handler it installs
falls back to `"%(message)s"`: no timestamp, no level, no logger name.

Two consequences the tests below are written against:

- **`configure_logging()` must force.** `FastMCP` is constructed at import of a server's `tools.py`
  and `connector_app` runs later, so a `basicConfig` without `force=True` is a no-op against a root
  logger that already has a handler — and the fix would have been silently ineffective.
- **An `mcp` release that stops calling `basicConfig` must not silence the fleet**, which is now
  true for the first time: this repository configures its own root logger either way.

The redaction tests are here rather than beside the sanitiser for the reason the sanitiser's own
docstring gives: `app.py`'s one fault line renders a traceback, and a traceback is where a
credential actually reaches a log.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest
from mcp.server.fastmcp import FastMCP
from mcp_server_kit.app import connector_app
from mcp_server_kit.identity import bind_caller, reset_caller
from mcp_server_kit.logging import (
    JsonFormatter,
    configure_logging,
    redact_secrets,
    register_secret_env,
)

TOKEN_ENV = "MCP_LOGGING_PROBE_TOKEN"
TOKEN = "s3cret-probe-token-value"


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    """Put the root logger back exactly as it was — every test here reconfigures it globally."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers = handlers
    root.setLevel(level)


def _captured(record_level: int = logging.INFO) -> tuple[logging.Handler, list[logging.LogRecord]]:
    """A handler that keeps the records it is given, carrying whatever filters were installed."""
    kept: list[logging.LogRecord] = []

    class Keeper(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            kept.append(record)

    handler = Keeper(record_level)
    return handler, kept


def test_fastmcp_still_configures_the_root_logger_behind_our_backs() -> None:
    """The upstream fact everything here is arranged around, measured against the installed `mcp`.

    Not read off the source: what matters is the *effect* — a root logger that has a handler and a
    format nobody in this repository chose. If an upstream release stops doing this, this test goes
    red and the paragraph above about `force=True` can be re-examined; until then, a fix that did
    not force would lose to it silently.
    """
    root = logging.getLogger()
    root.handlers = []
    FastMCP("probe-upstream-surface")
    assert root.handlers, (
        "mcp no longer configures the root logger at FastMCP construction; the ordering "
        "`configure_logging(force=True)` defends against may have changed"
    )
    formats = {handler.formatter._fmt for handler in root.handlers if handler.formatter}
    assert "%(message)s" in formats, (
        f"mcp's own basicConfig format is now {formats!r}; this fleet inherited '%(message)s' "
        "from it, which is why a WARNING and an INFO were byte-identical"
    )


def test_connector_app_wins_that_race(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server built the normal way ends up with *our* format, not upstream's.

    The whole point of `force=True`, asserted end to end in the order a real server does it:
    `FastMCP` first (as `tools.py` import does), `connector_app` second.
    """
    logging.getLogger().handlers = []
    monkeypatch.delenv("MCP_LOG_JSON", raising=False)
    monkeypatch.setenv("MCP_LOG_FORMAT", "%(levelname)s|%(name)s|%(message)s")
    server = FastMCP("probe-force")
    connector_app(server, name="probe-force")
    formats = {
        handler.formatter._fmt for handler in logging.getLogger().handlers if handler.formatter
    }
    assert formats == {"%(levelname)s|%(name)s|%(message)s"}


def test_a_line_carries_the_correlation_id_that_joins_it_to_the_audit_trail() -> None:
    """`ContextFilter`, and the field it exists for.

    The correlation id was bound on every request from the day the header existed and logged on
    none of them — its only readers in this repository were `identity.py` and its own test. A
    fleet's records are joined to Chemclaw3's audit trail by that string, and it was being
    populated in memory and dropped on the floor.
    """
    configure_logging()
    handler, kept = _captured()
    logging.getLogger().addHandler(handler)
    tokens = bind_caller("alice@example.test", "session-77", "corr-abc123")
    try:
        logging.getLogger("probe").info("something happened")
    finally:
        reset_caller(tokens)
    logging.getLogger().removeHandler(handler)

    assert len(kept) == 1
    assert kept[0].correlation == "corr-abc123"
    assert kept[0].actor == "alice@example.test"
    assert kept[0].session == "session-77"


def test_the_json_record_is_the_shape_chemclaw3_emits(monkeypatch: pytest.MonkeyPatch) -> None:
    """One system, one log shape — otherwise a cluster log stack parses half of it.

    Chemclaw3 emits `time`/`level`/`logger`/`source`/`correlation_id`/`actor`/`session_id` and a
    nested `fields`. A stack configured against that got unparseable bare strings from all seven
    pods of this fleet. The key names here are deliberately Chemclaw3's rather than this
    repository's contextvar names, because the point of matching is that one query answers over
    both halves.
    """
    monkeypatch.setenv("MCP_LOG_JSON", "true")
    configure_logging()
    handler, kept = _captured()
    logging.getLogger().addHandler(handler)
    tokens = bind_caller("bob@example.test", "session-9", "corr-9")
    try:
        logging.getLogger("probe").warning("a %s happened", "thing", extra={"tool": "echo"})
    finally:
        reset_caller(tokens)
    logging.getLogger().removeHandler(handler)

    payload = json.loads(JsonFormatter().format(kept[0]))
    assert payload["level"] == "WARNING"
    assert payload["logger"] == "probe"
    assert payload["message"] == "a thing happened"
    assert payload["correlation_id"] == "corr-9"
    assert payload["actor"] == "bob@example.test"
    assert payload["session_id"] == "session-9"
    assert payload["fields"] == {"tool": "echo"}
    assert payload["time"].endswith("+00:00"), "a naive local timestamp cannot be joined to a span"
    assert set(payload) >= {"time", "level", "logger", "source", "process", "thread", "message"}


def test_a_credential_does_not_survive_a_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    """The leak this filter was ported for, exercised on the path it actually happens on.

    `app.py`'s "a tool raised an unexpected exception" is `logger.exception`, which renders the
    traceback at *format* time — so a filter that rewrote only the message left the credential
    readable in the very line a failure produces. The audit's demo printed a DSN with its password
    to stdout through exactly this route.
    """
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    register_secret_env(TOKEN_ENV)
    configure_logging()
    handler, kept = _captured()
    formatter = logging.Formatter("%(message)s")
    logging.getLogger().addHandler(handler)
    try:
        raise RuntimeError(f"connecting with Authorization: Bearer {TOKEN}")
    except RuntimeError:
        logging.getLogger("probe").exception(
            "failed against postgresql://svc:hunter2pass@warehouse.internal:5432/eln"
        )
    logging.getLogger().removeHandler(handler)

    rendered = formatter.format(kept[0]) + (kept[0].exc_text or "")
    assert TOKEN not in rendered, "the server's own bearer token reached a log line"
    assert "hunter2pass" not in rendered, "a DSN password reached a log line"
    assert "warehouse.internal" in rendered, "redaction must keep what makes the line diagnostic"


def test_redaction_leaves_ordinary_chemistry_alone() -> None:
    """A rule that eats a molecule id is worse than the leak it closes.

    The structural patterns require a key-name or vendor anchor *and* a credential-shaped value for
    exactly this reason: an unreadable traceback is a permanent loss of the incident evidence.
    """
    for innocent in (
        "CC(=O)Oc1ccccc1C(=O)O",
        "solvent 2-MeTHF is not in the vendored table",
        "Bearer token was rejected",
        "the access_token field was absent",
    ):
        assert redact_secrets(innocent) == innocent, f"redaction corrupted {innocent!r}"


def test_the_level_is_an_environment_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verbosity without a code change — which the fleet had no way to do at all.

    The only knob was upstream's undocumented `FASTMCP_LOG_LEVEL`, and nothing in this repository
    or its deployment mentioned it.
    """
    monkeypatch.setenv("MCP_LOG_LEVEL", "debug")
    configure_logging()
    assert logging.getLogger().level == logging.DEBUG
