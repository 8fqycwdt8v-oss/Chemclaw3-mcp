"""Where a `tools/call`'s arguments are validated: on the loop when small, in a worker when large.

Held over a real socket and a real MCP session, because the size that decides it is the request's,
and the thread that validated is recorded by the argument model itself. The bounded refusal a large
call gets is held against what pydantic's own `errors()` says, so reading it off `json()` instead
cannot change a word.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel.server import request_ctx
from mcp_server_kit import validation
from mcp_server_kit.app import connector_app
from mcp_server_kit.refusal import MAX_REPORTED_ERRORS, validation_message
from mcp_server_kit.validation import INLINE_VALIDATION_MAX_BYTES
from pydantic import BaseModel, Field, ValidationError, field_validator

TOKEN = "test-token-for-the-validation-probe"
TOKEN_ENV = "MCP_KIT_VALIDATION_PROBE_TOKEN"

#: The thread the server's event loop runs on, recorded from the loop by `on_start`.
LOOP_THREAD: list[int] = []
#: The thread each validation of `Probe.text` ran on, in call order.
VALIDATED_ON: list[int] = []


class Probe(BaseModel):
    """One argument whose validation says which thread ran it."""

    text: str

    @field_validator("text")
    @classmethod
    def _record_thread(cls, value: str) -> str:
        VALIDATED_ON.append(threading.get_ident())
        return value


class Item(BaseModel):
    """Two required fields, so `{}` is two errors."""

    id: str = Field(min_length=1)
    text: str


async def _record_loop_thread() -> None:
    """Run once on the server's loop."""
    LOOP_THREAD.append(threading.get_ident())


def _probe_server() -> FastMCP:
    """One tool taking the recording model."""
    server = FastMCP("validation-probe")

    @server.tool()
    def measure(probe: Probe) -> dict[str, int]:
        """Return the length of what was passed."""
        return {"length": len(probe.text)}

    return server


@pytest.fixture(scope="module")
def running_server(serving: Callable[..., Any]) -> Iterator[str]:
    """The probe under uvicorn, with its loop thread recorded."""
    os.environ[TOKEN_ENV] = TOKEN
    app = connector_app(
        _probe_server(), name="validation-probe", token_env=TOKEN_ENV, on_start=_record_loop_thread
    )
    with serving(app, require_ready=True) as base:
        yield base


async def test_a_small_call_is_validated_on_the_loop_and_a_large_one_off_it(
    running_server: str, mcp_session: Callable[..., Any]
) -> None:
    """Up to `INLINE_VALIDATION_MAX_BYTES` the loop validates, as upstream; past it, a worker."""
    large = "x" * (INLINE_VALIDATION_MAX_BYTES + 1)
    VALIDATED_ON.clear()
    async with mcp_session(running_server, token=TOKEN) as session:
        small_answer = await session.call_tool("measure", {"probe": {"text": "x"}})
        large_answer = await session.call_tool("measure", {"probe": {"text": large}})
    assert small_answer.structuredContent == {"length": 1}
    assert large_answer.structuredContent == {"length": len(large)}
    assert LOOP_THREAD, "on_start never ran"
    small_thread, large_thread = VALIDATED_ON
    assert small_thread == LOOP_THREAD[0], "a small call left the loop, which upstream does not do"
    assert large_thread != LOOP_THREAD[0], (
        f"a {len(large)}-character argument was validated on the event loop; every stream on this "
        "pod waits while it is"
    )


async def test_a_large_invalid_call_is_a_bounded_refusal_not_a_fault(
    running_server: str, mcp_session: Callable[..., Any]
) -> None:
    """The failure leaves the worker as a `ValueError`, so the sanitiser passes it as a refusal."""
    async with mcp_session(running_server, token=TOKEN) as session:
        answer = await session.call_tool("measure", {"probe": {"txt": "x" * 70_000}})
    text = "".join(getattr(block, "text", "") for block in answer.content)
    assert answer.isError is True
    assert "1 validation error for measureArguments" in text
    assert "probe.text" in text
    assert "internal error" not in text, f"a validation failure was reported as a fault: {text}"
    assert "x" * 200 not in text, "the refusal quoted the caller's input back"


@pytest.mark.parametrize(
    ("headers", "large"),
    [
        ({"content-length": str(INLINE_VALIDATION_MAX_BYTES)}, False),
        ({"content-length": str(INLINE_VALIDATION_MAX_BYTES + 1)}, True),
        ({"transfer-encoding": "chunked"}, True),
    ],
    ids=["at-the-bound", "over-it", "undeclared"],
)
def test_a_request_that_declares_no_length_is_treated_as_large(
    headers: dict[str, str], large: bool
) -> None:
    """A chunked body says nothing about its size, so it is not trusted to be small."""
    serving: Any = SimpleNamespace(request=SimpleNamespace(headers=headers))
    token = request_ctx.set(serving)
    try:
        assert validation._carried_by_a_large_request() is large
    finally:
        request_ctx.reset(token)


def test_outside_an_mcp_request_validation_stays_inline() -> None:
    """A direct call, or `/rpc` (whose route validated its body itself), carries no MCP request."""
    assert validation._carried_by_a_large_request() is False


class _Batch(BaseModel):
    """A batch whose every item is wrong twice over."""

    items: list[Item]


@pytest.mark.parametrize("items", [1, MAX_REPORTED_ERRORS // 2, 5_000])
def test_the_bounded_message_names_what_errors_would_and_counts_the_rest(items: int) -> None:
    """Reading the errors off `json()` gives the lines `errors()` would, in its order."""
    with pytest.raises(ValidationError) as raised:
        _Batch.model_validate({"items": [{"id": ""}] * items})
    exc = raised.value
    expected = [
        f"{'.'.join(str(part) for part in error['loc'])}\n  {error['msg']} [type={error['type']}]"
        for error in exc.errors(include_input=False, include_url=False, include_context=False)
    ][:MAX_REPORTED_ERRORS]
    omitted = exc.error_count() - len(expected)
    if omitted:
        expected.append(f"... and {omitted} more")
    assert validation_message(exc) == "\n".join(
        [f"{exc.error_count()} validation errors for _Batch", *expected]
    )
