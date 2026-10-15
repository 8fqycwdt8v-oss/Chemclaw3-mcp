"""One authenticated request must not stall the event loop or earn an enormous answer.

The kubelet's liveness probe gives `/livez` three seconds (`servers/*/deploy/deployment.yaml`), so a
single request that blocks the loop for that long takes a pod out of service. These are the shapes
that did, on `/rpc` and on `/mcp`:

- a body of tens of thousands of unknown keys (one validation error each, formatted whole and then
  scanned for secrets);
- a batch of empty items (two errors each), whose validation alone is seconds of work;
- a body nested so deeply that `json.loads` raises `RecursionError`.

Each is asserted on the answer's size and on how long the event loop stalled.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, ClassVar

import httpx
import pytest
from chemclaw_contracts.rpc import RpcError, WireRequest
from mcp.server.fastmcp import FastMCP
from mcp.types import PARSE_ERROR
from mcp_server_kit.app import connector_app
from pydantic import BaseModel, Field

TOKEN = "test-token-for-the-hostile-bodies"
TOKEN_ENV = "MCP_KIT_HOSTILE_PROBE_TOKEN"

#: Under the kubelet's three seconds for `/livez` and well under the four to five seconds the
#: unbounded handling stalled the loop for. A batch's 400,000 errors are built by pydantic in
#: single calls that hold the GIL, so its budget leaves room for a loaded runner (1.5 s measured).
LOOP_LAG_BUDGET_SECONDS = 1.0
BATCH_LOOP_LAG_BUDGET_SECONDS = 2.5
HEARTBEAT_SECONDS = 0.01
#: A refusal names a handful of errors; this is generous for that and nothing like the megabytes
#: the unbounded message was.
ANSWER_BUDGET_CHARS = 5_000
ATTACK_KEYS = 70_000
ATTACK_ITEMS = 200_000
MCP_ATTACK_ITEMS = 20_000
ATTACK_DEPTH = 400_000


class Item(BaseModel):
    """One batch element; both fields are required, so `{}` is two errors."""

    id: str = Field(min_length=1)
    text: str


class EchoRequest(WireRequest):
    """`echo`."""

    tool_name: ClassVar[str] = "echo"
    text: str


class BatchRequest(WireRequest):
    """`batch`."""

    tool_name: ClassVar[str] = "batch"
    items: list[Item]


def _probe_server() -> FastMCP:
    """Two tools: one flat argument, one batch of models."""
    server = FastMCP("hostile-probe")

    @server.tool()
    def echo(text: str) -> dict[str, str]:
        """Return what was passed in."""
        return {"text": text}

    @server.tool()
    def batch(items: list[Item]) -> dict[str, int]:
        """Count the items."""
        return {"n": len(items)}

    return server


@pytest.fixture(scope="module")
def running_server(serving: Callable[..., Any]) -> Iterator[str]:
    """The probe under uvicorn with the kit's default body cap, which is what a backend ships."""
    os.environ[TOKEN_ENV] = TOKEN
    app = connector_app(
        _probe_server(),
        name="hostile-probe",
        token_env=TOKEN_ENV,
        on_start=_heartbeat,
        rpc={"echo": EchoRequest, "batch": BatchRequest},
    )
    with serving(app, require_ready=True) as base:
        yield base


LOOP_LAG: list[float] = []


async def _heartbeat() -> None:
    """Run on the server's own loop, recording how late each 10 ms sleep woke: the loop's stall.

    Measured from inside the loop, because a client polling `/livez` from this process would be
    starved by the GIL it shares with the validation thread, which no deployed pod's kubelet is.
    """
    while True:
        started = time.perf_counter()
        await asyncio.sleep(HEARTBEAT_SECONDS)
        LOOP_LAG.append(time.perf_counter() - started - HEARTBEAT_SECONDS)


@contextmanager
def _watching_the_loop() -> Iterator[list[float]]:
    """The lags recorded while the block runs, and only those."""
    LOOP_LAG.clear()
    yield LOOP_LAG


def _rpc(base: str, operation: str, content: bytes) -> httpx.Response:
    """POST `content` to `/rpc/<operation>` with the credential."""
    return httpx.post(
        f"{base}/rpc/{operation}",
        content=content,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        timeout=60.0,
    )


def test_many_unknown_keys_are_refused_small_and_do_not_stall_the_loop(running_server: str) -> None:
    """70,000 unknown keys are 70,000 errors; the answer names a few and the loop stays prompt."""
    body: dict[str, object] = {f"k{n}": 1 for n in range(ATTACK_KEYS)}
    body["text"] = "x"
    content = json.dumps(body).encode()
    with _watching_the_loop() as lag:
        response = _rpc(running_server, "echo", content)
    assert lag, "the heartbeat never ran"
    assert max(lag) < LOOP_LAG_BUDGET_SECONDS, f"the loop stalled {max(lag):.2f} s"
    assert response.status_code == 400
    assert RpcError.model_validate(response.json()).kind == "refused"
    assert len(response.text) < ANSWER_BUDGET_CHARS, f"a {len(response.text)}-character refusal"


def test_the_refusal_still_says_what_was_wrong(running_server: str) -> None:
    """Bounded is not empty: the first errors are named, and the rest are counted."""
    body: dict[str, object] = {f"k{n}": 1 for n in range(50)}
    body["text"] = "x"
    message = RpcError.model_validate(
        _rpc(running_server, "echo", json.dumps(body).encode()).json()
    )
    assert "50 validation errors" in message.message
    assert "k0" in message.message
    assert "more" in message.message


def test_a_batch_of_empty_items_is_refused_small_and_does_not_stall_the_loop(
    running_server: str,
) -> None:
    """The same defence for the nested shape: two errors per empty item."""
    content = json.dumps({"items": [{}] * ATTACK_ITEMS}).encode()
    with _watching_the_loop() as lag:
        response = _rpc(running_server, "batch", content)
    assert response.status_code == 400
    assert len(response.text) < ANSWER_BUDGET_CHARS, f"a {len(response.text)}-character refusal"
    assert max(lag) < BATCH_LOOP_LAG_BUDGET_SECONDS, f"the loop stalled {max(lag):.2f} s"


async def test_the_mcp_path_bounds_the_same_messages(
    running_server: str, mcp_session: Callable[..., Any]
) -> None:
    """`tools/call` with a batch of empty items gets a short refusal, not upstream's whole text.

    Only the message is bounded here; validating the batch still runs on the loop on this path.
    """
    async with mcp_session(running_server, token=TOKEN) as session:
        result = await session.call_tool("batch", {"items": [{}] * MCP_ATTACK_ITEMS})
    assert result.isError is True
    text = "".join(getattr(block, "text", "") for block in result.content)
    assert len(text) < ANSWER_BUDGET_CHARS, f"a {len(text)}-character refusal"
    assert "validation error" in text


def test_a_body_nested_past_the_parser_is_a_refusal_not_a_traceback(running_server: str) -> None:
    """`[[[[...` raises `RecursionError` in `json.loads`, which is not a `ValueError`."""
    response = _rpc(running_server, "echo", b"[" * ATTACK_DEPTH)
    assert response.status_code == 400
    assert RpcError.model_validate(response.json()).kind == "refused"


def test_the_mcp_surface_answers_a_deeply_nested_body_with_a_400(running_server: str) -> None:
    """`NonFiniteLiteralRefusal` parses `/mcp` bodies too, so it answers the one it cannot."""
    response = httpx.post(
        f"{running_server}/mcp",
        content=b"[" * ATTACK_DEPTH,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        timeout=60.0,
    )
    assert response.status_code == 400, f"{response.status_code}: {response.text[:200]}"
    assert response.json()["error"]["code"] == PARSE_ERROR


def test_a_control_character_in_a_path_cannot_forge_a_log_line(
    running_server: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A percent-encoded newline in the request path is logged escaped, on one line."""
    with caplog.at_level(logging.INFO, logger="mcp_server_kit.auth"):
        httpx.post(f"{running_server}/rpc/x%0aFORGED%0dline", json={}, timeout=10.0)
        httpx.post(
            f"{running_server}/rpc/x%0aFORGED%0dline",
            json={},
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=10.0,
        )
    lines = [record.getMessage() for record in caplog.records if record.name.endswith("auth")]
    assert len(lines) >= 2, "no access lines were logged"
    for line in lines:
        assert "\n" not in line
        assert "\r" not in line
    assert any("FORGED" in line for line in lines)
