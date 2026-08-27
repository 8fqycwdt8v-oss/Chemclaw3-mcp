"""The fleet's own log configuration — because until this file existed there was none.

**What was actually configuring logging here.** Nothing in `packages/` or `servers/` called
`basicConfig`, `dictConfig` or read a level from the environment. Log lines appeared anyway, and
that was the trap: `FastMCP.__init__` calls `configure_logging(...)`, which calls
`logging.basicConfig(level=..., format="%(message)s", handlers=[RichHandler(...)])` — and `rich`
is not installed in any image here, so the handler falls back to a bare `StreamHandler` with the
format `"%(message)s"`. Uvicorn's own `dictConfig` does not touch the root logger (measured: root
stays at WARNING), so that constructor was the whole of it.

Everything downstream of that followed from a library's constructor rather than from a decision:

- **No timestamp on any line.** A `WARNING` and an `INFO` were byte-identical, because the format
  carried neither the level nor the logger name either.
- **No verbosity knob** except upstream's undocumented `FASTMCP_LOG_LEVEL`.
- **No redaction.** A traceback carrying a DSN password printed it.
- **An `mcp` release that drops that `basicConfig` call silences seven pods**, with nothing red.

And one cost that is not about any single server: Chemclaw3 emits JSON with `time`/`level`/`logger`
/`correlation_id`/`actor`/`session_id`, so a cluster log stack configured to parse it got
unparseable bare strings from every pod in this fleet. **The record shape here is deliberately the
same one** (`chemclaw.core.logging.JsonFormatter`), so the two halves of one system are one stream.

**`configure_logging()` is called from `connector_app` with `force=True`**, which is the whole
reason it works regardless of import order: `FastMCP` is constructed at `tools.py` import time,
long before `app.py` runs, and `basicConfig` without `force` is a no-op once the root has a
handler. `tests/test_logging.py` asserts that ordering against the *installed* `mcp` — the
`test_upstream_surface.py` habit Chemclaw3 keeps for exactly this class of coupling.

Three knobs, `MCP_`-prefixed like every other variable this fleet reads: `MCP_LOG_LEVEL`,
`MCP_LOG_FORMAT`, `MCP_LOG_JSON`.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime
from typing import Any

from mcp_server_kit.identity import current_caller

__all__ = [
    "ContextFilter",
    "JsonFormatter",
    "SecretRedactingFilter",
    "configure_logging",
    "redact_secrets",
    "register_secret_env",
    "structured_fields",
]

LEVEL_ENV = "MCP_LOG_LEVEL"
FORMAT_ENV = "MCP_LOG_FORMAT"
JSON_ENV = "MCP_LOG_JSON"

# The same shape Chemclaw3's `log_format` default carries, for the same reason it carries it: the
# three identifiers belong in the format a developer actually reads, not only in the JSON one that
# is set in a chart and nowhere else.
DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(correlation)s/%(session)s]: %(message)s"
DEFAULT_LEVEL = "INFO"

_REDACTED = "***"
# Below this length a "secret" is more likely a placeholder or a word that occurs in ordinary
# prose, and redacting it would corrupt every line containing that substring.
_MIN_REDACTABLE = 8

# Environment variables whose *values* must never appear in a log line. Names, never values: a
# rotated credential must be redacted on the next line, so `os.environ` is read per call.
#
# A set rather than a constant list because a server's bearer-token variable is named by its own
# `connector.yaml` and passed to `connector_app`, which registers it here. Registration is
# idempotent and additive; nothing removes.
_SECRET_ENVS: set[str] = set()


def register_secret_env(name: str) -> None:
    """Add an environment variable to this process's redaction inventory, for its whole life.

    Called by `connector_app` with the server's `token_env`, so the credential a server checks on
    every request is scrubbed from every line it emits — including the tracebacks, which is where
    a credential actually reaches a log.
    """
    if name:
        _SECRET_ENVS.add(name)


def _secret_values() -> tuple[str, ...]:
    """The distinct secret values this process holds, longest first.

    Longest first so a DSN is redacted before the password inside it: replacing the shorter one
    first would leave a mangled DSN still naming the host and the user.

    Read fresh from `os.environ` on every call rather than memoised, for the reason Chemclaw3
    measured and recorded: a value that becomes secret mid-process must be redacted on the *next*
    line, not on the next line after a cache window expires.
    """
    values = {os.environ.get(name, "") for name in _SECRET_ENVS}
    return tuple(sorted((v for v in values if len(v) >= _MIN_REDACTABLE), key=len, reverse=True))


# A credential carried in a URL's userinfo — `scheme://user:secret@host`, which is how a password
# reaches a DSN and a token reaches a git remote. Matched structurally because this is the class the
# value inventory cannot cover: the credential belongs to something outside this process, so there
# is no environment variable to look for. The user is kept, so a redacted line still says which
# principal and which host failed.
_URL_USERINFO = re.compile(
    r"([a-zA-Z][a-zA-Z0-9+.\-]{0,63}://)([^/\s:@]{0,512})(?::([^/\s@]{0,512}))?@"
)

# The characters a credential is made of. No quotes, parens, commas or semicolons: those are what a
# repr, a call expression or a libpq string puts *around* a value, never inside one. Without that
# exclusion a key-name rule eats the source lines of this repository, which are precisely the text
# that appears in the tracebacks this mechanism exists to protect.
_OPAQUE = r"[A-Za-z0-9_\-.~+/=]"
# Not preceded by a token character. `\b` matches between `-` and `e`, so every `-eyJ` in a hostile
# string would be a fresh start position whose tail rescans the remainder — quadratic, on a path
# that holds the stdlib logging lock.
_NOT_MID_TOKEN = r"(?<![A-Za-z0-9_\-.])"
# "Contains a digit" — the cheap discriminator between a credential and an identifier. Bounded for
# the same reason `_NOT_MID_TOKEN` exists: every anchor scans at most 255 characters instead of the
# rest of the line.
_HAS_DIGIT = r"(?=" + _OPAQUE + r"{0,255}\d)"

_STRUCTURAL_SECRETS: tuple[re.Pattern[str], ...] = (
    # Vendor-assigned prefixes decisive on their own.
    re.compile(_NOT_MID_TOKEN + r"gh[pousr]_[A-Za-z0-9]{20,255}"),
    re.compile(_NOT_MID_TOKEN + r"github_pat_[A-Za-z0-9_]{20,255}"),
    re.compile(_NOT_MID_TOKEN + r"sk-(?:ant|proj|svcacct)-[A-Za-z0-9_\-]{16,255}"),
    re.compile(_NOT_MID_TOKEN + r"sk-[A-Za-z0-9]{32,255}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,255}"),
    # A JWT — three base64url segments, the first starting `eyJ` because a JOSE header always
    # begins `{"`. This is the shape of the bearer Chemclaw3's front door holds.
    re.compile(
        _NOT_MID_TOKEN + r"eyJ[A-Za-z0-9_\-]{8,1024}\.[A-Za-z0-9_\-]{8,4096}\."
        r"[A-Za-z0-9_\-]{1,1024}"
    ),
    # libpq key/value connection strings and the environment spelling. The URL form is
    # `_URL_USERINFO`'s.
    re.compile(
        r"(?P<keep>\b(?:PG)?PASSWORD[\"']?\s*[=:]\s*[\"']?)" + _HAS_DIGIT + _OPAQUE + r"{6,255}",
        re.IGNORECASE,
    ),
    # A credential in a query string, a header or a rendered dict, anchored on the key name so the
    # bare words "token" and "secret" in prose cannot fire, and on the value's shape so an
    # assignment in a source line cannot either.
    re.compile(
        r"(?P<keep>\b\w*?(?:access_token|refresh_token|api[_-]?key|client_secret|token|secret"
        r"|private_key|passwd|pwd)"
        r"[\"']?\s*[=:]\s*[\"']?)" + _HAS_DIGIT + _OPAQUE + r"{8,255}",
        re.IGNORECASE,
    ),
    # The SCREAMING_CASE environment spelling, which the rule above structurally cannot reach: `_`
    # is a word character, so `\bsecret` does not match inside `AWS_SECRET_ACCESS_KEY`.
    re.compile(
        r"(?P<keep>\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*"
        r"_?(?:SECRET|TOKEN|PASSWORD|PASSWD|APIKEY|CREDENTIAL)"
        r"[A-Z0-9_]*[\"']?\s*[=:]\s*[\"']?)" + _OPAQUE + r"{8,255}"
    ),
    # `Authorization: Basic <base64>`. Base64 of `user:password` need not contain a digit, so this
    # rule deliberately does not require one; the header anchor carries the whole specificity.
    re.compile(r"(?P<keep>\bAuthorization:\s*Basic\s+)[A-Za-z0-9+/=]{8,4096}", re.IGNORECASE),
    # An opaque bearer has no internal structure, so the scheme is the anchor and the digit
    # requirement is what keeps "Bearer token was rejected" intact.
    re.compile(r"(?P<keep>\b(?:Bearer|Token)\s+)" + _HAS_DIGIT + _OPAQUE + r"{16,4096}"),
)


def _redact_structural(match: re.Match[str]) -> str:
    """Replace a structurally-matched credential, keeping the label that names it."""
    return f"{match.groupdict().get('keep') or ''}{_REDACTED}"


def _redact_userinfo(match: re.Match[str]) -> str:
    """Replace a URL's credential, keeping the scheme and (where there is one) the user."""
    scheme, first, second = match.group(1), match.group(2), match.group(3)
    if second is None:
        return f"{scheme}{_REDACTED}@"
    return f"{scheme}{first}:{_REDACTED}@"


def redact_secrets(text: str) -> str:
    """Return `text` with every credential this process can recognise replaced by `***`.

    Exposed rather than private because anything that *persists* an error message — a stored
    failure reason, a diagnostic written to a file — should apply the same redaction the log path
    applies, and two spellings of "scrub a credential" is how one of them goes stale.
    """
    redacted = text
    for secret in _secret_values():
        redacted = redacted.replace(secret, _REDACTED)
    # A callable replacement, not a `\1` template: a template is compiled lazily by the `re`
    # machinery on first use, and that compilation does `import re` — on the logging path, which
    # must import nothing (a filter can run from inside another module's import).
    redacted = _URL_USERINFO.sub(_redact_userinfo, redacted)
    for pattern in _STRUCTURAL_SECRETS:
        redacted = pattern.sub(_redact_structural, redacted)
    return redacted


# Every attribute `logging` itself puts on a record. Anything else in `record.__dict__` arrived
# through `extra=` — which is exactly what `structured_fields` exists to find. Written as a literal
# rather than derived from a probe record, because a probe misses the attributes `logging` adds
# conditionally (`exc_text`, `stack_info`, `taskName`), and the failure mode of missing one is that
# an internal attribute is published as if it were a caller's field.
_LOGRECORD_RESERVED = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
    # `ContextFilter`'s own three: stamped by this module onto every record and promoted to
    # top-level keys by `JsonFormatter`, so they are not a caller's fields and must not be swept
    # twice.
    | {"actor", "correlation", "session"}
)

# Set by `SecretRedactingFilter` once it has swept a record, and read by `JsonFormatter` so the
# formatter does not redact the same strings a second time. The formatter keeps its own pass for
# the case the mark is absent — a handler carrying no filter — which must not become a leak
# because this optimisation exists.
_REDACTED_MARK = "_mcp_redacted"

# Renders `exc_info` for the filter. Module scope so the logging path constructs nothing per
# record; a bare `Formatter` because only its `formatException` is used.
_EXC_RENDERER = logging.Formatter()


def structured_fields(record: logging.LogRecord) -> dict[str, object]:
    """The fields a caller attached with `extra=`, and nothing `logging` put there itself.

    One definition, used by both the redaction filter (which must scrub them) and the JSON
    formatter (which must publish them). Two spellings of "which keys are the caller's" is exactly
    how one of them comes to publish an attribute the other never scrubbed.
    """
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _LOGRECORD_RESERVED and not key.startswith("_")
    }


class ContextFilter(logging.Filter):
    """Stamp the calling turn's actor, session and correlation id onto every record.

    The correlation id is the field that joins this fleet's lines to Chemclaw3's audit trail, and
    before this filter existed it was bound on every request (`identity.bind_caller`) and read by
    nothing — populated in memory and dropped on the floor. The only readers of
    `current_caller().correlation` in the whole repository were `identity.py` itself and its test.

    `setdefault`, not assignment: a caller that passes one of these through `extra=` is doing so
    precisely because the ambient value is wrong at that moment.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Add the three identifiers, `-` where there is none, and always keep the record."""
        caller = current_caller()
        record.__dict__.setdefault("correlation", caller.correlation or "-")
        record.__dict__.setdefault("actor", caller.actor or "-")
        record.__dict__.setdefault("session", caller.session or "-")
        return True


class SecretRedactingFilter(logging.Filter):
    """Replace any credential this process holds with `***` in a record's rendered message.

    A filter rather than a formatter, because a deployment may install its own formatter and
    redaction must not be something a formatting choice can switch off. It runs on the *rendered*
    message so a secret passed as a `%s` argument is caught too — `logger.info("dsn=%s", dsn)`
    keeps the value in `record.args` until format time, which is how one escapes a filter that
    only inspects `record.msg`.

    **The traceback is the field that mattered.** `logger.exception(...)` renders the exception at
    format time, so a filter that rewrote only the message left every credential readable in the
    very lines a failure produces — and a failure is exactly when a connection string or an auth
    header ends up inside the error text. `app.py`'s "a tool raised an unexpected exception" was
    measured printing a DSN with its password.

    **Nothing here may raise**, which is why the whole of it sits in a `try`. Filters run inside
    `Handler.handle` but *outside* the try/except that wraps `emit()`, so an exception here lands
    in whoever called `logger.info(...)`. Keeping the record is the right answer rather than the
    merely safe-looking one: a record this filter cannot process is one the formatter cannot
    process either, so it goes on to logging's own error path, which is what happens with no filter
    installed at all.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact in place and always keep the record."""
        # `try`/`except`/`pass` rather than `contextlib.suppress`, which is what SIM105 asks for:
        # this runs once per record while the stdlib logging lock is held, and `suppress` allocates
        # and enters a context manager every time. Nothing else about the two differs here.
        try:  # noqa: SIM105
            self._redact(record)
        except Exception:
            pass
        return True

    def _redact(self, record: logging.LogRecord) -> None:
        """Rewrite every text field of `record` in place, for the well-formed case."""
        message = record.getMessage()
        redacted = redact_secrets(message)
        if redacted != message:
            # Collapsed to a plain message: the args have been folded in, and leaving them would
            # let a formatter re-render the original.
            record.msg = redacted
            record.args = None
        # Truthiness, not `is not None`: `logger.error(..., exc_info=False)` puts the *bool* on the
        # record, and `formatException` subscripts it.
        if record.exc_info and record.exc_text is None:
            record.exc_text = _EXC_RENDERER.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_secrets(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_secrets(record.stack_info)
        for key, value in structured_fields(record).items():
            if isinstance(value, str):
                scrubbed = redact_secrets(value)
                if scrubbed != value:
                    setattr(record, key, scrubbed)
        record.__dict__[_REDACTED_MARK] = True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, in the record shape Chemclaw3's log stack already parses.

    The fields are the ones a query starts from: when, how bad, from where, and the three
    identifiers that join a line to the audit trail (`correlation_id`), to a conversation
    (`session_id`) and to a person (`actor`). An exception goes into `exception` rather than
    trailing after the line, because a multi-line traceback in a line-delimited format is how a
    stack trace becomes forty unparseable entries.

    `exception` is taken from `record.exc_text` and never re-rendered from `exc_info`: re-rendering
    would reach past `SecretRedactingFilter` into the original exception and emit the credential
    the filter had already replaced.

    The key names are Chemclaw3's (`correlation_id`, `session_id`) rather than this repository's
    contextvar names, deliberately — the point of matching the shape is that one query answers over
    both halves of the system.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Render one record as a compact JSON object."""
        swept = record.__dict__.get(_REDACTED_MARK, False)
        message = record.getMessage()
        payload: dict[str, Any] = {
            # ISO-8601 in UTC with an explicit offset. `formatTime` gives naive *local* time in a
            # comma-millisecond format, so every join between a log line and a `timestamptz` goes
            # through a lossy parse and a guess at the pod's zone.
            "time": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            # `logger` names a module; `source` is what turns a log search into a code location.
            "source": f"{record.module}.{record.funcName}:{record.lineno}",
            "process": record.process,
            "thread": record.threadName,
            "message": message if swept else redact_secrets(message),
            "correlation_id": getattr(record, "correlation", "-"),
            "actor": getattr(record, "actor", "-"),
            "session_id": getattr(record, "session", "-"),
        }
        # Nested under `fields` rather than merged at the top level, so a caller cannot shadow
        # `level`, `time` or `correlation_id` — a field named `level` arriving from a tool result
        # would otherwise rewrite the severity a log stack routes on.
        fields = structured_fields(record)
        if fields:
            payload["fields"] = (
                fields
                if swept
                else {
                    key: redact_secrets(value) if isinstance(value, str) else value
                    for key, value in fields.items()
                }
            )
        if record.exc_text:
            payload["exception"] = record.exc_text
        elif record.exc_info:
            payload["exception"] = redact_secrets(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack"] = record.stack_info if swept else redact_secrets(record.stack_info)
        return json.dumps(payload, default=str)


def _truthy(raw: str | None) -> bool:
    """Whether an environment variable spells "on" in any of the shapes an operator writes."""
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}


def configure_logging(*, force: bool = True) -> None:
    """Configure the root logger from the environment, and put the filters on every handler.

    `force=True` by default and that is the point: `FastMCP.__init__` has already called
    `basicConfig` by the time any server's `app.py` runs, so without it this function is a silent
    no-op and every line in the fleet keeps upstream's bare `"%(message)s"`. It also makes the
    function idempotent — a second call re-applies the configured settings rather than stacking
    duplicate handlers.

    The filters go on the *handlers* rather than on a logger, because a filter attached to a logger
    is not consulted for records that propagate up from a child, and every module here logs through
    `getLogger(__name__)`. On the handler, nothing reaches an output stream unfiltered.

    Args:
        force: Replace any handlers the root already has. Pass `False` only to layer this on top of
            a configuration a caller established deliberately.
    """
    logging.basicConfig(
        level=(os.environ.get(LEVEL_ENV) or DEFAULT_LEVEL).upper(),
        format=os.environ.get(FORMAT_ENV) or DEFAULT_FORMAT,
        force=force,
    )
    as_json = _truthy(os.environ.get(JSON_ENV))
    context, redaction = ContextFilter(), SecretRedactingFilter()
    for handler in _handlers_that_reach_an_output_stream():
        # `force=True` resets the *root's* handlers, so a second call starts them clean — but a
        # non-propagating logger's handlers are not ours to reset and would otherwise accumulate a
        # pair per call, running redaction N times per record.
        if not any(isinstance(existing, SecretRedactingFilter) for existing in handler.filters):
            handler.addFilter(context)
            handler.addFilter(redaction)
        if as_json:
            handler.setFormatter(JsonFormatter())


def _handlers_that_reach_an_output_stream() -> list[logging.Handler]:
    """Every handler a record can reach — the root's, plus any non-propagating logger's own.

    "Put the filter on the root handler" is complete only while every record propagates to the
    root, and uvicorn is the process where that is false: `uvicorn.Config.__init__` runs its own
    `dictConfig` and gives `uvicorn` a handler with `propagate: false`, so `uvicorn.error` — which
    logs every unhandled ASGI exception *with* `exc_info`, i.e. exactly the records that carry a
    DSN or an auth header — reaches a stream this module would never have touched.

    The sweep is one-shot: it walks the manager once, so a non-propagating logger created *after*
    this call is not reached. What makes it work for uvicorn is an ordering fact — its
    `configure_logging()` runs before the app is built — not a general property.
    """
    handlers: list[logging.Handler] = list(logging.getLogger().handlers)
    # Neither a root handler nor any logger's own, and it is what a non-propagating logger with no
    # handlers of its own falls back to — an ordinary library shape.
    if logging.lastResort is not None:
        handlers.append(logging.lastResort)
    # Snapshot under a single C-level copy: `loggerDict` is mutated by `getLogger()`, and this runs
    # while a lazy import on another thread may be creating one. A comprehension over the live view
    # can observe that mid-iteration and raise.
    for existing in list(logging.root.manager.loggerDict.values()):
        # `PlaceHolder` entries are not loggers and carry no handlers.
        if isinstance(existing, logging.Logger) and not existing.propagate:
            handlers.extend(existing.handlers)
    return handlers
