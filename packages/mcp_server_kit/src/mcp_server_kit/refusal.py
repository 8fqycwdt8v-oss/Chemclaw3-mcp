"""What a failed tool call may tell its caller: whether it may be read at all, and how much of it.

`is_caller_safe` is the one discriminator behind the sanitiser, the `refused`/`failed` counter, the
span status and `/rpc`'s 400-versus-500. `bounded_message` and `validation_message` are the one
bound on the text: a validation error is one entry per offending field, so a body of tens of
thousands of unknown keys would otherwise be formatted whole, scanned for secrets, and sent back.

Invariants: a message a caller sees is at most `MAX_REFUSAL_CHARS` long, and names at most
`MAX_REPORTED_ERRORS` validation errors, with the rest counted.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError

__all__ = [
    "MAX_REFUSAL_CHARS",
    "MAX_REPORTED_ERRORS",
    "bounded_message",
    "is_caller_safe",
    "validation_message",
]

#: How many validation errors a refusal names; the count says how many there were.
MAX_REPORTED_ERRORS = 10

#: The longest message a caller is told, in characters. Far above any deliberate refusal in the
#: fleet and far below the megabytes a hostile body can make pydantic format.
MAX_REFUSAL_CHARS = 2_000


def is_caller_safe(exc: ToolError) -> bool:
    """Whether this `ToolError` is a refusal the model may read, rather than a fault to hide.

    Caller-safe: a chained `ValueError` (including pydantic's `ValidationError`), or upstream's
    unchained `Unknown tool` error; a failing tool body otherwise arrives chained to its real cause.
    """
    return exc.__cause__ is None or isinstance(exc.__cause__, ValueError)


def bounded_message(text: str) -> str:
    """`text`, cut to `MAX_REFUSAL_CHARS` with its full length said, before anything scans it."""
    if len(text) <= MAX_REFUSAL_CHARS:
        return text
    return f"{text[:MAX_REFUSAL_CHARS]} ... [cut: {len(text)} characters]"


def validation_message(exc: ValidationError) -> str:
    """The first `MAX_REPORTED_ERRORS` errors of `exc`, never the input they quote, and the count.

    Decoded one at a time off `exc.json()`, so nothing a caller sent is echoed except the field
    names it chose. Not `str(exc)` or `errors()`: each builds every error as text or as objects,
    over a second holding the GIL for a hostile body's 300,000, where the JSON is a sixth of that.
    """
    count = exc.error_count()
    rendered = exc.json(include_input=False, include_url=False, include_context=False)
    decoder = json.JSONDecoder()
    errors: list[dict[str, Any]] = []
    position = rendered.index("[") + 1
    while len(errors) < min(count, MAX_REPORTED_ERRORS):
        while rendered[position] in ", \t\r\n":
            position += 1
        error, position = decoder.raw_decode(rendered, position)
        errors.append(error)
    lines = [
        f"{'.'.join(str(part) for part in error['loc']) or '<body>'}\n  {error['msg']}"
        f" [type={error['type']}]"
        for error in errors
    ]
    noun = "error" if count == 1 else "errors"
    omitted = count - len(lines)
    if omitted > 0:
        lines.append(f"... and {omitted} more")
    return bounded_message(f"{count} validation {noun} for {exc.title}\n" + "\n".join(lines))
