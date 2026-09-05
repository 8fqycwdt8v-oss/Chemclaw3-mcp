"""The `X-Chemclaw-*` header contract with Chemclaw3, checked against the names it actually sends.

Every other cross-repository claim this fleet makes is checked. `assert_manifest_matches` drives a
running server and pins the tool surface in both directions, because "a `connector.yaml` is a
*claim* about the tool surface, and Chemclaw3's own history is a list of claims that outlived the
code they described". The four identity headers are the same kind of claim and had no such check —
so one of them was wrong, on every request, for as long as it had existed.

`HEADER_CORRELATION` read `x-chemclaw-correlation` against a sender writing
`X-Chemclaw-Correlation-Id`. HTTP header lookup is case-insensitive, not suffix-insensitive, so
`request.headers.get(...)` returned `None` and `bind_caller` bound the empty string.

**It never surfaced as a *broken* thing, which is not the same as nothing reading it.** This
paragraph said nothing consumed `current_caller().correlation`, and `mcp_server_kit.logging`'s
`ContextFilter` reads all three fields onto **every log record** in every server — installed by
`configure_logging` on each handler that reaches an output stream. So the wrong spelling did reach
production behaviour: it wrote `correlation=-` on every line of every server, on the one field that
joins this fleet's records to Chemclaw3's audit trail, and a missing id reads as "this request
carried none" rather than as a defect. What no server does *yet* is stamp a stored record with it —
`current_caller` is public kit API, and the first one to do so would have written an empty string
into a durable row. That is the shape Chemclaw3 named in
`D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution`: a provenance field nothing
can fill, described in the present tense by three docstrings.

**The literals below are deliberately not imported from `mcp_server_kit.identity`.** A test written
against this repository's own constants asserts that this repository agrees with itself, which it
always did — the two constants were consistent, and both were wrong about the sender. These strings
are transcribed from `chemclaw.connectors.identity` — exactly its `STAMPED_HEADERS` tuple
(`HEADER_ACTOR`, `HEADER_SESSION`, `HEADER_CORRELATION`, `HEADER_DRY_RUN`) — and are the thing under
test. Change one only because Chemclaw3 changed it.

**`X-Chemclaw-Roles` was a fifth and is not one any more.** Chemclaw3 deleted `HEADER_ROLES` in
`D-2026-08-26-an-entitlement-set-is-not-provenance`: it had one writer and, measured across both
repositories, zero readers, while being the one header with no bound — under
`entra_group_claims_as_roles` it carried every AD group a user is in, to every connector. This file
went on listing it among the constants it transcribes and went on sending it, which is precisely the
failure it exists to catch, in the file positioned as the authority. Nothing broke at runtime,
because the header was ignored on both sides; what was lost is the property that made this file
worth having.

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

import anyio
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
SENT_SESSION = "X-Chemclaw-Session"
SENT_CORRELATION = "X-Chemclaw-Correlation-Id"
SENT_DRY_RUN = "X-Chemclaw-Dry-Run"

# Chemclaw3's `STAMPED_HEADERS`, in its order. Its own comment says a new `X-Chemclaw-*` header
# belongs in that tuple the day it is written, so transcribing the whole tuple rather than four
# loose names is what makes a fifth one a visible gap here rather than a silent one.
SENT_STAMPED = (SENT_ACTOR, SENT_SESSION, SENT_CORRELATION, SENT_DRY_RUN)

ACTOR = "alice-oid"
SESSION = "sess-1"
CORRELATION = "corr-abc"


#: What each `slow_whoami` body read, appended from the server thread's loop — the concurrent
#: test's evidence, since two raw POSTs' SSE bodies are harder to parse than one shared list.
SEEN_CONCURRENT: list[tuple[str, str, str]] = []


def _probe_app() -> uvicorn.Config:
    """A `connector_app` whose tools report the caller their own bodies can see."""
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

    @server.tool()
    async def slow_whoami() -> str:
        """Dawdle long enough that two calls overlap, then record the caller this body sees."""
        await anyio.sleep(0.25)
        caller = current_caller()
        SEEN_CONCURRENT.append((caller.actor, caller.session, caller.correlation))
        return "ok"

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


async def test_two_concurrent_calls_on_one_session_each_read_their_own_caller(
    probe_url: str,
) -> None:
    """Chemclaw3's agent gathers a whole tool batch, so two `tools/call`s can be in flight on one
    `mcp-session-id` at once — and the caller re-binding was only ever measured sequentially.

    If the SDK served both from one task, the bind/reset pairs would interleave and a durable row
    would be stamped with the other caller's identity. This pins that each in-flight call reads
    its own headers, which is the property the fleet's whole attribution story rests on under
    parallel batches. Raw JSON-RPC posts rather than `ClientSession`, because the client session
    fixes its headers at construction and the thing under test is per-*call* identity.
    """

    def who(name: str) -> dict[str, str]:
        return {
            SENT_ACTOR: f"{name}-oid",
            SENT_SESSION: f"sess-{name}",
            SENT_CORRELATION: f"corr-{name}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }

    SEEN_CONCURRENT.clear()
    async with httpx.AsyncClient(timeout=10.0) as client:
        opened = await client.post(
            probe_url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "probe", "version": "1"},
                },
            },
            headers=who("opener"),
        )
        session_id = opened.headers["mcp-session-id"]
        await client.post(
            probe_url,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={**who("opener"), "mcp-session-id": session_id},
        )

        async def call(name: str, request_id: int) -> None:
            await client.post(
                probe_url,
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {"name": "slow_whoami", "arguments": {}},
                },
                headers={**who(name), "mcp-session-id": session_id},
            )

        async with anyio.create_task_group() as group:
            group.start_soon(call, "alice", 2)
            group.start_soon(call, "bob", 3)

    assert sorted(SEEN_CONCURRENT) == [
        ("alice-oid", "sess-alice", "corr-alice"),
        ("bob-oid", "sess-bob", "corr-bob"),
    ], (
        f"two concurrent calls on one session read {SEEN_CONCURRENT}; the per-call binding "
        "interleaved and a stamped record would carry the other caller's identity"
    )


def test_the_constants_are_exactly_the_headers_chemclaw3_stamps() -> None:
    """The inverse assertion, which is what makes the four above a *set* rather than four names.

    A header Chemclaw3 adds is invisible here otherwise, and a header it deletes lives on — this
    file sent `X-Chemclaw-Roles` on every request for as long as after Chemclaw3 removed it
    (`D-2026-08-26-an-entitlement-set-is-not-provenance`), while claiming in its own docstring to
    transcribe the sender's constants. Both directions, so neither can drift alone.
    """
    from mcp_server_kit import identity

    ours = {
        identity.HEADER_ACTOR,
        identity.HEADER_SESSION,
        identity.HEADER_CORRELATION,
        identity.HEADER_DRY_RUN,
    }
    assert ours == {sent.lower() for sent in SENT_STAMPED}
