"""The `X-Chemclaw-*` header contract with Chemclaw3, checked against the names it actually sends.

Every other cross-repository claim this fleet makes is checked. `assert_manifest_matches` drives a
running server and pins the tool surface in both directions, because "a `connector.yaml` is a
*claim* about the tool surface, and Chemclaw3's own history is a list of claims that outlived the
code they described". The four identity headers are the same kind of claim and had no such check —
so one of them was wrong, on every request, for as long as it had existed.

`HEADER_CORRELATION` read `x-chemclaw-correlation` against a sender writing
`X-Chemclaw-Correlation-Id`. HTTP header lookup is case-insensitive, not suffix-insensitive, so
`request.headers.get(...)` returned `None` and `bind_caller` bound the empty string. Nothing in
`servers/` consumes `current_caller().correlation` yet, which is the only reason it never surfaced;
`current_caller` is public kit API, and the first server to stamp a record with it would have
written an empty string into the one field that joins this fleet's records to Chemclaw3's audit
trail. That is the shape Chemclaw3 named in
`D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution`: a provenance field nothing
can fill, described in the present tense by three docstrings.

**The literals below are deliberately not imported from `mcp_server_kit.identity`.** A test written
against this repository's own constants asserts that this repository agrees with itself, which it
always did — the two constants were consistent, and both were wrong about the sender. These strings
are transcribed from `chemclaw.connectors.identity` (`HEADER_ACTOR`, `HEADER_ROLES`,
`HEADER_SESSION`, `HEADER_CORRELATION`, `HEADER_DRY_RUN`) and are the thing under test. Change one
only because Chemclaw3 changed it.

Driven through a real `connector_app` over a real MCP session rather than through the middleware
alone, because the caller is bound *twice* and only the second binding is what a tool body reads:
middleware binds the ASGI task, `_bind_caller_per_tool_call` re-binds inside the session manager's.
A test that only exercised the first would have passed while a tool read the handshake's identity —
which is the defect that made per-tool re-binding necessary in the first place.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP
from mcp_server_kit import connector_app, current_caller

# Transcribed from `chemclaw.connectors.identity`. See the module docstring for why these are
# literals rather than imports.
SENT_ACTOR = "X-Chemclaw-Actor"
SENT_ROLES = "X-Chemclaw-Roles"
SENT_SESSION = "X-Chemclaw-Session"
SENT_CORRELATION = "X-Chemclaw-Correlation-Id"
SENT_DRY_RUN = "X-Chemclaw-Dry-Run"

ACTOR = "alice-oid"
SESSION = "sess-1"
CORRELATION = "corr-abc"


def _probe_app() -> uvicorn.Config:
    """A `connector_app` whose one tool reports the caller its own body can see."""
    server = FastMCP("identity-probe")

    @server.tool()
    def whoami() -> dict[str, str]:
        """Report the caller bound for this tool call."""
        caller = current_caller()
        return {
            "actor": caller.actor,
            "session": caller.session,
            "correlation": caller.correlation,
        }

    app = connector_app(server, name="identity-probe", token_env=None)
    return uvicorn.Config(app, host="127.0.0.1", port=_free_port(), log_level="warning")


def _free_port() -> int:
    """An ephemeral loopback port, released immediately for uvicorn to claim."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def probe_url() -> Iterator[str]:
    """Run the probe server under uvicorn on loopback and yield its MCP endpoint.

    A real socket rather than an ASGI transport, matching every server's own `test_server.py`.
    Two reasons, and the second is not a preference: the MCP transport's DNS-rebinding guard
    refuses a synthetic `Host`, and driving the session manager's lifespan from the test's own task
    exits an `anyio` cancel scope in a task that did not enter it. Loopback, so the egress guard
    permits it.
    """
    config = _probe_app()
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://{config.host}:{config.port}"
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/healthz", timeout=1.0).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:  # pragma: no cover - the server failed to come up at all
        raise RuntimeError("the identity probe server did not start")
    yield f"{base}/mcp"
    server.should_exit = True


async def _call_whoami(url: str, headers: dict[str, str]) -> dict[str, str]:
    """Open a real MCP session with `headers` and return what the tool body read."""
    async with (
        httpx.AsyncClient(headers=headers) as client,
        streamable_http_client(url, http_client=client) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool("whoami", {})
        assert not result.isError
        payload: dict[str, str] = json.loads(getattr(result.content[0], "text", "{}"))
        return payload


async def test_a_tool_body_reads_every_header_chemclaw3_sends(probe_url: str) -> None:
    """All three identity values reach the tool body under the sender's own spellings.

    The regression this pins: `correlation` came back `""` here while the other two were fine, so
    any test that checked "identity arrives" without naming each field would have passed.
    """
    seen = await _call_whoami(
        probe_url,
        {
            SENT_ACTOR: ACTOR,
            SENT_ROLES: "process-chemist",
            SENT_SESSION: SESSION,
            SENT_CORRELATION: CORRELATION,
            SENT_DRY_RUN: "false",
        },
    )
    assert seen == {"actor": ACTOR, "session": SESSION, "correlation": CORRELATION}


async def test_an_absent_header_is_empty_rather_than_missing(probe_url: str) -> None:
    """A caller off the request path sends no identity, and that must not be an error.

    Chemclaw3 omits a header rather than sending an empty one precisely so a server's log cannot
    claim an anonymous user made the call, so the absent case is a real one and has to be typed the
    same way as the present one.
    """
    seen = await _call_whoami(probe_url, {})
    assert seen == {"actor": "", "session": "", "correlation": ""}


@pytest.mark.parametrize(
    ("constant", "sent"),
    [
        ("HEADER_ACTOR", SENT_ACTOR),
        ("HEADER_SESSION", SENT_SESSION),
        ("HEADER_CORRELATION", SENT_CORRELATION),
        ("HEADER_DRY_RUN", SENT_DRY_RUN),
    ],
)
def test_each_constant_names_the_header_that_is_sent(constant: str, sent: str) -> None:
    """The constants agree with the sender, case aside — the one-line version of the test above.

    Kept beside the end-to-end check rather than instead of it: this one names *which* constant
    drifted, which is the thing a reader of a red suite wants first. Neither is sufficient alone —
    a constant can be right while the binding is not, and `HEADER_DRY_RUN` has no binding at all
    (it is read straight off the request in `CallerLogMiddleware`), so only this covers it.
    """
    from mcp_server_kit import identity

    assert getattr(identity, constant) == sent.lower()
