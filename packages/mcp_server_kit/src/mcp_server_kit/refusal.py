"""Whether a failed tool call is a refusal the caller may read, or a fault to hide.

The one discriminator behind the sanitiser, the `refused`/`failed` metric split, the trace span's
status and the RPC route's 400-versus-500, so the four cannot disagree.
"""

from __future__ import annotations

from mcp.server.fastmcp.exceptions import ToolError

__all__ = ["is_caller_safe"]


def is_caller_safe(exc: ToolError) -> bool:
    """Whether this `ToolError` is a refusal the model may read, rather than a fault to hide.

    Caller-safe: a chained `ValueError` (including pydantic's `ValidationError`), or upstream's
    unchained `Unknown tool` error; a failing tool body otherwise arrives chained to its real cause.
    """
    return exc.__cause__ is None or isinstance(exc.__cause__, ValueError)
