"""The fleet's own log configuration: format, level, redaction and the caller's identifiers.

Without it, the only configuration is `FastMCP.__init__`'s `basicConfig` with a bare
`"%(message)s"` — no timestamp, no level, no redaction. The JSON record shape matches Chemclaw3's
(`chemclaw.core.logging.JsonFormatter`) so both halves of the system are one log stream.

`configure_logging()` runs from the app's lifespan with `force=True`: `force` overrides upstream's
earlier `basicConfig`, and the lifespan (not import) keeps importing a server free of side effects.
Knobs: `MCP_LOG_LEVEL`, `MCP_LOG_FORMAT`, `MCP_LOG_JSON`.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
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

# Chemclaw3's default shape: the three identifiers belong in the text format too, not only in JSON.
DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(correlation)s/%(session)s]: %(message)s"
DEFAULT_LEVEL = "INFO"

_REDACTED = "***"
# Below this length a "secret" is more likely a placeholder or a word that occurs in ordinary
# prose, and redacting it would corrupt every line containing that substring.
_MIN_REDACTABLE = 8

# Environment variables whose values must never appear in a log line. Names, not values: values are
# read per call so a rotated credential is redacted on the next line. Registration is additive.
_SECRET_ENVS: set[str] = set()


def register_secret_env(name: str) -> None:
    """Add an environment variable to this process's redaction inventory, for its whole life.

    `connector_app` registers the server's `token_env`, so the bearer secret is scrubbed from every
    line, tracebacks included.
    """
    if name:
        _SECRET_ENVS.add(name)


# Secret-shaped values this repository publishes (the `make run-*` default token), so not secrets;
# redacting them only corrupts logs. `tests/test_fleet.py` holds this against the Makefile.
_PUBLISHED_VALUES = frozenset({"dev-token"})


def _dsn_password(value: str) -> str:
    """The password inside a `scheme://user:password@host` DSN, or `""`.

    Matched on its own too, because an error may quote only the credential, not the DSN.
    """
    if "://" not in value or "@" not in value:
        return ""
    userinfo = value.split("://", 1)[1].split("@", 1)[0]
    return userinfo.split(":", 1)[1] if ":" in userinfo else ""


def _secret_values() -> tuple[str, ...]:
    """The distinct secret values this process holds, longest first.

    Longest first so a DSN is redacted before the password inside it. Read fresh from `os.environ`
    each call so a newly secret value is redacted on the next line.
    """
    values: set[str] = set()

    def consider(candidate: str) -> None:
        """Add `candidate` unless it is too short to match safely, or published in this repo."""
        if len(candidate) >= _MIN_REDACTABLE and candidate not in _PUBLISHED_VALUES:
            values.add(candidate)

    for name in _SECRET_ENVS:
        value = os.environ.get(name, "")
        consider(value)
        consider(_dsn_password(value))
    return tuple(sorted(values, key=len, reverse=True))


# A credential in a URL's userinfo (`scheme://user:secret@host`), matched structurally because it
# belongs to something outside this process. The user is kept so the line still names the principal.
_URL_USERINFO = re.compile(
    r"([a-zA-Z][a-zA-Z0-9+.\-]{0,63}://)([^/\s:@]{0,512})(?::([^/\s@]{0,512}))?@"
)

# Characters a credential is made of — no quotes, parens, commas or semicolons, which surround a
# value in a repr or source line, so key-name rules do not eat traceback source lines.
_OPAQUE = r"[A-Za-z0-9_\-.~+/=]"
# Not preceded by a token character; `\b` would restart mid-token and make matching quadratic
# while the logging lock is held.
_NOT_MID_TOKEN = r"(?<![A-Za-z0-9_\-.])"  # noqa: S105 - a lookbehind named for what it guards
# "Contains a digit", the cheap credential/identifier discriminator, bounded to 255 characters.
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
    # A credential in a query string, header or dict, anchored on the key name and the value's shape
    # so
    # prose and source lines do not fire.
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

    Public so anything that persists or serves an error message applies the same redaction.
    """
    redacted = text
    for secret in _secret_values():
        redacted = redacted.replace(secret, _REDACTED)
    # A callable replacement, not a `\1` template, whose lazy compile would import on the logging
    # path.
    redacted = _URL_USERINFO.sub(_redact_userinfo, redacted)
    for pattern in _STRUCTURAL_SECRETS:
        redacted = pattern.sub(_redact_structural, redacted)
    return redacted


# Every attribute `logging` itself puts on a record, conditional ones included; anything else came
# through `extra=`. A literal, since a probe record misses the conditional attributes.
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
    # `ContextFilter`'s own fields, promoted to top-level keys by `JsonFormatter`.
    | {"actor", "correlation", "session"}
)

# Set by `SecretRedactingFilter` after sweeping a record so `JsonFormatter` skips a second pass; the
# formatter still redacts when the mark is absent (a handler without the filter).
_REDACTED_MARK = "_mcp_redacted"

# Renders `exc_info` for the filter. Module scope so the logging path constructs nothing per
# record; a bare `Formatter` because only its `formatException` is used.
_EXC_RENDERER = logging.Formatter()


def structured_fields(record: logging.LogRecord) -> dict[str, object]:
    """The fields a caller attached with `extra=`, and nothing `logging` put there itself.

    One definition shared by the redaction filter and the JSON formatter, so what is published is
    what was scrubbed.
    """
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _LOGRECORD_RESERVED and not key.startswith("_")
    }


class ContextFilter(logging.Filter):
    """Stamp the calling turn's actor, session and correlation id onto every record.

    `setdefault`, so a value passed through `extra=` wins over the ambient one.
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

    A filter, so a deployment's own formatter cannot switch redaction off. It redacts the rendered
    message (catching `%s` arguments) and the rendered traceback, where credentials actually appear.
    It never raises — filters run outside `emit()`'s error handling — and a record it cannot process
    is kept and reaches the redacting `handleError`.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact in place and always keep the record."""
        # Not `contextlib.suppress`: this runs per record under the logging lock.
        try:  # noqa: SIM105
            self._redact(record)
        # S110/BLE001: a filter that raises sends the unredacted record to logging's error path.
        except Exception:  # noqa: S110, BLE001
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


def _redacted_for_diagnostic(value: object) -> str:
    """One field of logging's own error diagnostic, rendered and scrubbed, never raising.

    `repr` for a non-string, as `handleError` would print; `***` if even rendering raises, since the
    input already failed to render once.
    """
    try:
        return redact_secrets(value if isinstance(value, str) else repr(value))
    # BLE001: a hostile `__repr__` is expected here; narrowing would leak a second exception.
    except Exception:  # noqa: BLE001
        return _REDACTED


def _install_redacting_handle_error(handler: logging.Handler) -> None:
    r"""Bind a `handleError` on `handler` that scrubs the record before stderr sees it.

    The filter is fail-open, so a record it cannot render fails in `emit` too, and CPython's
    `handleError` prints the raw `msg` and `args` — where a credential lives — to stderr. Not
    `logging.raiseExceptions = False`, which would hide every handler failure. Idempotent: delegates
    to `type(handler).handleError`, so rebinding never stacks.
    """

    def handle_error(record: logging.LogRecord) -> None:
        """Print logging's own diagnostic for `record` with its credentials removed.

        Nothing here may raise: a scrub failure drops the arguments, and the delegation always runs.
        """
        msg, args = record.msg, record.args
        try:
            record.msg = _redacted_for_diagnostic(msg)
            if isinstance(args, tuple):
                record.args = tuple(_redacted_for_diagnostic(arg) for arg in args)
            elif isinstance(args, Mapping):
                record.args = {key: _redacted_for_diagnostic(value) for key, value in args.items()}
            elif args is not None:
                record.args = _redacted_for_diagnostic(args)
        # BLE001: same reason as `_redacted_for_diagnostic` above - already inside the error path.
        except Exception:  # noqa: BLE001
            # An unscrubbable argument is dropped, never printed: this runs because formatting
            # already failed once, so the value is exactly the kind that might carry a secret.
            record.args = None
        try:
            type(handler).handleError(handler, record)
        finally:
            # Restored: the same record goes to every handler, which must format the caller's own
            # values.
            record.msg, record.args = msg, args

    handler.handleError = handle_error  # type: ignore[method-assign]


class JsonFormatter(logging.Formatter):
    """One JSON object per line, in the record shape Chemclaw3's log stack already parses.

    Carries time, level, logger, and `correlation_id`/`session_id`/`actor` under Chemclaw3's key
    names. `exception` comes from the already-redacted `record.exc_text`, never re-rendered from
    `exc_info`, which would bypass redaction.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Render one record as a compact JSON object."""
        swept = record.__dict__.get(_REDACTED_MARK, False)
        message = record.getMessage()
        payload: dict[str, Any] = {
            # ISO-8601 in UTC with an explicit offset, unlike `formatTime`'s naive local time.
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
        # Nested under `fields` so a caller's field cannot shadow `level`, `time` or
        # `correlation_id`.
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

    `force=True` overrides the `basicConfig` upstream already ran, and makes repeat calls
    idempotent.
    Filters go on handlers, not loggers, because logger filters skip propagated records; every
    handler also gets a redacting `handleError`.

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
        # A non-propagating logger's handlers are not reset by `force`, so do not add the filters
        # twice.
        if not any(isinstance(existing, SecretRedactingFilter) for existing in handler.filters):
            handler.addFilter(context)
            handler.addFilter(redaction)
        # Unconditionally: a record the fail-open filter could not process ends up here. Idempotent.
        _install_redacting_handle_error(handler)
        if as_json:
            handler.setFormatter(JsonFormatter())


def _handlers_that_reach_an_output_stream() -> list[logging.Handler]:
    """Every handler a record can reach — the root's, plus any non-propagating logger's own.

    uvicorn's `uvicorn` logger does not propagate, and `uvicorn.error` logs tracebacks. One-shot:
    loggers created after this call are not reached; uvicorn configures its own before the app runs.
    """
    handlers: list[logging.Handler] = list(logging.getLogger().handlers)
    # Neither a root handler nor any logger's own, and it is what a non-propagating logger with no
    # handlers of its own falls back to — an ordinary library shape.
    if logging.lastResort is not None:
        handlers.append(logging.lastResort)
    # Snapshot with one C-level copy; another thread's `getLogger()` may mutate `loggerDict`.
    for existing in list(logging.root.manager.loggerDict.values()):
        # `PlaceHolder` entries are not loggers and carry no handlers.
        if isinstance(existing, logging.Logger) and not existing.propagate:
            handlers.extend(existing.handlers)
    return handlers
