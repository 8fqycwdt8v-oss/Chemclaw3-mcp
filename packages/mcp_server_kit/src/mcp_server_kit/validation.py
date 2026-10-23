"""Validate a large `tools/call`'s arguments in a worker thread, and bound a failure there.

Upstream's `Tool.run` validates on the event loop and formats a failure whole into its message
before `connector_app`'s sanitiser cuts it: a ~600 KB batch of empty items is 300,000 errors and a
47 MB string. `offload_large_argument_validation` gives every tool a `FuncMetadata` whose
`call_fn_with_arg_validation` — upstream's lines, pinned in `tests/test_upstream_surface.py` —
validates in a worker thread when the MCP request carrying the call is over
`INLINE_VALIDATION_MAX_BYTES` or declares no length, and raises a failure there as a `ValueError`
holding `refusal.validation_message`'s bounded text. A smaller call runs upstream's method itself.

The worker still holds the GIL through each pydantic call, so the loop stalls for the longest single
one, not for none. A `/rpc` call carries no MCP request: its route validated the body against the
contract model already, off the loop when large, and this validates it again inline.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.func_metadata import FuncMetadata
from mcp.server.lowlevel.server import request_ctx
from pydantic import ValidationError

from mcp_server_kit.refusal import validation_message

__all__ = ["INLINE_VALIDATION_MAX_BYTES", "offload_large_argument_validation"]

#: Bodies up to this size are validated on the event loop, where it costs microseconds. A larger one
#: is validated in a worker thread: a hostile body is hundreds of thousands of validation errors,
#: seconds of work that would stall `/livez` if the loop did it.
INLINE_VALIDATION_MAX_BYTES = 65_536


def _carried_by_a_large_request() -> bool:
    """Whether the MCP request serving this call is over `INLINE_VALIDATION_MAX_BYTES`.

    A request declaring no length (chunked) counts as large, since nothing says it is small. Outside
    an MCP request — a direct call, or `/rpc` — `False`.
    """
    headers = getattr(getattr(request_ctx.get(None), "request", None), "headers", None)
    if headers is None:
        return False
    try:
        return int(headers.get("content-length", "")) > INLINE_VALIDATION_MAX_BYTES
    except ValueError:
        return True


def _validated(metadata: FuncMetadata, arguments_to_validate: dict[str, Any]) -> dict[str, Any]:
    """Upstream's three validation lines, for the worker thread; a failure leaves already bounded.

    Raises:
        ValueError: the arguments are invalid; the message is `validation_message`'s, built here so
            neither the validation nor its formatting runs on the event loop.
    """
    try:
        arguments_pre_parsed = metadata.pre_parse_json(arguments_to_validate)
        arguments_parsed_model = metadata.arg_model.model_validate(arguments_pre_parsed)
    except ValidationError as exc:
        raise ValueError(validation_message(exc)) from None
    return arguments_parsed_model.model_dump_one_level()


class _OffloadedValidation(FuncMetadata):
    """A tool's `FuncMetadata`, validating a large call in a worker thread."""

    async def call_fn_with_arg_validation(
        self,
        fn: Callable[..., Any | Awaitable[Any]],
        fn_is_async: bool,
        arguments_to_validate: dict[str, Any],
        arguments_to_pass_directly: dict[str, Any] | None,
    ) -> Any:
        """Upstream's validate-then-call, the validation moved off the loop for a large call.

        The tool body is called where upstream calls it, on the loop for a synchronous tool.
        """
        if not _carried_by_a_large_request():
            return await super().call_fn_with_arg_validation(
                fn, fn_is_async, arguments_to_validate, arguments_to_pass_directly
            )
        arguments_parsed_dict = await asyncio.to_thread(_validated, self, arguments_to_validate)
        arguments_parsed_dict |= arguments_to_pass_directly or {}
        if fn_is_async:
            return await fn(**arguments_parsed_dict)
        return fn(**arguments_parsed_dict)


def offload_large_argument_validation(server: FastMCP) -> None:
    """Give every tool `server` serves an `_OffloadedValidation` copy of its `FuncMetadata`.

    `Tool.run` reads `fn_metadata` on every call. Every field is copied, so the argument model
    `finite.refuse_non_finite_arguments` installed is the one validated, whichever ran first.
    """
    for tool in server._tool_manager.list_tools():
        metadata = tool.fn_metadata
        tool.fn_metadata = _OffloadedValidation.model_construct(
            **{name: getattr(metadata, name) for name in type(metadata).model_fields}
        )
