"""What a log line in this fleet carries, and that this fleet decides it.

`FastMCP.__init__` calls upstream's `configure_logging`, installing a bare `"%(message)s"`
handler at import of a server's `tools.py`, before `connector_app` runs. So:

- **`configure_logging()` must force**, or it is a no-op against the existing root handler.
- **An `mcp` release that stops calling `basicConfig` must not silence the fleet.**

The redaction tests live here because a traceback rendered in a log line is where a credential
actually leaks.
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
    """The installed `mcp` still configures the root logger at `FastMCP` construction.

    If upstream stops, this goes red and the `force=True` reasoning can be re-examined.
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
    """A server started the normal way ends up with *our* format, not upstream's.

    `FastMCP` first (as importing `tools.py` does), then the app's `lifespan` via `TestClient`,
    since the configuration runs at startup, not at app construction.
    """
    from fastapi.testclient import TestClient

    logging.getLogger().handlers = []
    monkeypatch.delenv("MCP_LOG_JSON", raising=False)
    monkeypatch.setenv("MCP_LOG_FORMAT", "%(levelname)s|%(name)s|%(message)s")
    server = FastMCP("probe-force")
    app = connector_app(server, name="probe-force")
    with TestClient(app) as client:
        client.get("/healthz")
    formats = {
        handler.formatter._fmt for handler in logging.getLogger().handlers if handler.formatter
    }
    assert formats == {"%(levelname)s|%(name)s|%(message)s"}


def test_building_an_app_does_not_reconfigure_the_importing_process() -> None:
    """`connector_app` is called at module scope by every server, so it must not touch the root.

    Building the app must leave the importing process's handlers, level and `lastResort` alone, so
    the kit can be embedded in a test runner or script. The configuration runs in the `lifespan`.
    """
    root = logging.getLogger()
    root.handlers = []
    host = logging.StreamHandler()
    root.addHandler(host)
    root.setLevel(logging.DEBUG)
    last_resort_filters = len(logging.lastResort.filters) if logging.lastResort else 0

    connector_app(FastMCP("probe-import-purity"), name="probe-import-purity")

    assert host in root.handlers, "building an app tore out a handler its host had installed"
    assert root.level == logging.DEBUG, "building an app changed its host's root log level"
    assert (len(logging.lastResort.filters) if logging.lastResort else 0) == last_resort_filters, (
        "building an app filtered the interpreter's `lastResort` handler"
    )


def test_a_line_carries_the_correlation_id_that_joins_it_to_the_audit_trail() -> None:
    """`ContextFilter` puts the correlation id on every line.

    That string joins this fleet's records to Chemclaw3's audit trail.
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
    # The three names are what `ContextFilter` *injects*, so they live in the record's `__dict__`
    # rather than on `LogRecord` — reading them there is what the formatter does too.
    bound = vars(kept[0])
    assert bound["correlation"] == "corr-abc123"
    assert bound["actor"] == "alice@example.test"
    assert bound["session"] == "session-77"


def test_the_json_record_is_the_shape_chemclaw3_emits(monkeypatch: pytest.MonkeyPatch) -> None:
    """One system, one log shape — otherwise a cluster log stack parses half of it.

    The JSON keys are Chemclaw3's (`time`/`level`/`logger`/`source`/`correlation_id`/`actor`/
    `session_id`, nested `fields`) so one query answers over both.
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
    """A credential does not survive in a rendered traceback.

    `logger.exception` renders the traceback at format time, so a filter that rewrote only the
    message would leave the credential in the very line a failure produces.
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


def test_loggings_own_error_path_does_not_print_the_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Logging's own error path does not print the credential.

    When a record cannot be formatted, `Handler.handleError` prints `record.msg` and `record.args`
    to stderr before any redaction, and `args` is where a `%s` credential lives. Driven by an
    ordinary `%`-format mismatch. The diagnostic itself must survive, so this is a scrub, not
    `raiseExceptions = False`.
    """
    import io
    import sys

    dsn = "postgresql://chemclaw:supersecret123@db.internal:5432/chemclaw"
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    register_secret_env(TOKEN_ENV)
    configure_logging()

    captured = io.StringIO()
    monkeypatch.setattr(sys, "stderr", captured)
    # `%d` against a string: `getMessage()` raises, the filter swallows it and keeps the record,
    # and the formatter then fails the same way.
    logging.getLogger("probe").info("connecting to %s as %d", dsn, TOKEN)
    printed = captured.getvalue()

    assert "Message:" in printed, (
        "logging's diagnostic disappeared; the fix must scrub the record, not silence the report "
        "of a handler that could not emit"
    )
    assert "supersecret123" not in printed, "a DSN password reached stderr through handleError"
    assert TOKEN not in printed, "the server's own bearer token reached stderr through handleError"
    assert "db.internal" in printed, "the diagnostic must stay diagnostic after redaction"


def test_the_published_dev_token_is_not_treated_as_a_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A credential anybody can read in the `Makefile` is not a credential, and redacting it lies.

    The published `dev-token` default is exempt from redaction, as are published DSN passwords.
    """
    monkeypatch.setenv(TOKEN_ENV, "dev-token")
    register_secret_env(TOKEN_ENV)
    line = "the dev-token default is what `make run-props` uses"
    assert redact_secrets(line) == line, (
        "the repository's own published dev credential was scrubbed out of its own logs"
    )


def test_a_dsn_password_is_redacted_on_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DSN's password is redacted on its own, without the DSN around it.

    libpq quotes the bare credential with no `PASSWORD=` anchor. `_dsn_password` is one function so
    the inventory and the published-defaults set decide the same thing about the same string.
    """
    dsn_env = "MCP_LOGGING_PROBE_DSN"
    monkeypatch.setenv(dsn_env, "postgresql://svc:hunter2pass@warehouse.internal:5432/eln")
    register_secret_env(dsn_env)
    redacted = redact_secrets("the credential hunter2pass was rejected by warehouse.internal")
    assert "hunter2pass" not in redacted
    assert "warehouse.internal" in redacted, "redaction must keep what makes the line diagnostic"
