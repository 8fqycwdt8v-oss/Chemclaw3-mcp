"""How many MCP sessions this pod will hold, and what it does with the handshake that arrives full.

`test_sessions.py` covers how long one session lives; this covers how many exist at once.
Upstream admits sessions without bound and an idle timeout cannot see a burst, so without a
ceiling the pod is OOMKilled. Properties:

- **The refusal is prompt** — no queueing until a session frees.
- **The refusal mints nothing.**
- **A full pod still serves the sessions it has.**
- **A session costs what the ceiling was derived from**, checked by measurement.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp_server_kit.app import connector_app
from mcp_server_kit.metrics import SESSIONS_REFUSED
from mcp_server_kit.sessions import (
    AT_CAPACITY_RETRY_AFTER_SECONDS,
    AT_CAPACITY_STATUS,
    DEFAULT_MAX_SESSIONS,
    REFUSAL_LOG_INTERVAL_SECONDS,
    SESSION_BACKLOG_BUDGET_BYTES,
    SESSION_COST_BYTES,
    SMALLEST_POD_MEMORY_LIMIT_BYTES,
    max_sessions,
)

TOKEN = "test-token-for-the-session-ceiling"
TOKEN_ENV = "MCP_KIT_CEILING_TOKEN"
PROTOCOL_VERSION = "2025-06-18"

#: The ceiling the driven probes run at. Small enough that saturating it is a second of test time,
#: and larger than one so "full" is a state the pod reaches rather than its initial condition.
PROBE_CEILING = 8

#: How many handshakes arrive *together* at the already-full pod. More than one because a single
#: refusal cannot distinguish "refused promptly" from "got lucky on an empty queue".
PROBE_BURST = 12

#: How many handshakes arrive together against an **empty** pod in the burst probe. Much wider than
#: the ceiling, because what it is looking for is how far past the bound a concurrent caller gets,
#: and a width of ceiling-plus-one could only ever show one extra.
BURST_WIDTH = PROBE_CEILING * 8

#: How much slower than an admitted handshake a refused one may be. A ratio, because the harness's
#: own thread and connection setup dominate any absolute time; a refusal does no work, so it cannot
#: be slower than an admission. The slack is for scheduling noise, not a queue.
MAX_REFUSAL_OVER_ADMISSION = 2.0

#: Sessions opened by the cost probe, and the warm-up that precedes them. The warm-up outlasts the
#: allocator's first-touch growth and the freed small-object arenas earlier tests leave in the
#: process, so the measured sessions are charged their resident cost rather than placed in holes.
COST_PROBE_SESSIONS = 400
COST_PROBE_WARMUP = 600


def _probe_server(name: str) -> FastMCP:
    """A server with one instant tool — nothing here is about what a tool does."""
    server = FastMCP(name)

    @server.tool()
    def echo(text: str) -> str:
        """Return what was passed in."""
        return text

    return server


@pytest.fixture
def token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bearer credential every app in this file requires."""
    monkeypatch.setenv(TOKEN_ENV, TOKEN)


@pytest.fixture
def full_pod(
    monkeypatch: pytest.MonkeyPatch, token: None, serving: Callable[..., Any]
) -> Iterator[tuple[str, FastMCP]]:
    """A server at `PROBE_CEILING`, saturated, with its own `FastMCP` for counting.

    The ceiling is read when `connector_app` builds the app, so the variable is set before that.
    """
    monkeypatch.setenv("MCP_MAX_SESSIONS", str(PROBE_CEILING))
    server = _probe_server("ceiling")
    app = connector_app(server, name="ceiling", token_env=TOKEN_ENV)
    with serving(app) as base:
        for _ in range(PROBE_CEILING):
            _open_session(base)
        yield base, server


def _handshake(base: str, client: httpx.Client | None = None) -> httpx.Response:
    """One raw `initialize` POST — the request that mints a session, with no client library.

    `client` is shared only by the cost probe, which opens hundreds; every other probe wants an
    independent connection.
    """
    post = client.post if client is not None else httpx.post
    return post(
        f"{base}/mcp",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "0"},
            },
        },
        timeout=30.0,
    )


def _open_session(base: str, client: httpx.Client | None = None) -> str:
    """Open a session and return its id, failing loudly if the pod refused it."""
    response = _handshake(base, client)
    if response.status_code != 200:
        pytest.fail(f"a handshake that should have been admitted got {response.status_code}")
    return response.headers["mcp-session-id"]


def _rss_bytes() -> int:
    """This process's resident set size, from `/proc` — no dependency, and the pod's own number."""
    with open("/proc/self/status", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    raise RuntimeError("/proc/self/status carries no VmRSS line")


def test_the_default_is_derived_from_the_smallest_pod_and_the_measured_session_cost() -> None:
    """The default ceiling is re-derived from the session cost and the smallest pod's budget.

    Changing `SESSION_COST_BYTES` or `DEFAULT_MAX_SESSIONS` alone fails. The fleet tests check the
    smallest pod limit against shipped Deployments.
    """
    assert SESSION_BACKLOG_BUDGET_BYTES == SMALLEST_POD_MEMORY_LIMIT_BYTES // 8
    assert DEFAULT_MAX_SESSIONS * SESSION_COST_BYTES <= SESSION_BACKLOG_BUDGET_BYTES, (
        f"{DEFAULT_MAX_SESSIONS} sessions at {SESSION_COST_BYTES} B is "
        f"{DEFAULT_MAX_SESSIONS * SESSION_COST_BYTES} B, over the "
        f"{SESSION_BACKLOG_BUDGET_BYTES} B budget an eighth of the smallest pod allows"
    )
    # And not absurdly under it either: a ceiling of 1 would satisfy the line above and refuse the
    # fleet's own traffic. The budget is a bound the default is meant to approach.
    assert DEFAULT_MAX_SESSIONS * SESSION_COST_BYTES > SESSION_BACKLOG_BUDGET_BYTES // 2


def test_the_ceiling_is_an_environment_variable_and_zero_turns_it_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The knob, including the escape hatch a deployment would take only deliberately."""
    monkeypatch.delenv("MCP_MAX_SESSIONS", raising=False)
    assert max_sessions() == DEFAULT_MAX_SESSIONS
    monkeypatch.setenv("MCP_MAX_SESSIONS", "64")
    assert max_sessions() == 64
    monkeypatch.setenv("MCP_MAX_SESSIONS", "0")
    assert max_sessions() is None
    monkeypatch.setenv("MCP_MAX_SESSIONS", "not a number")
    assert max_sessions() == DEFAULT_MAX_SESSIONS


def _burst(base: str, width: int) -> list[tuple[int, float]]:
    """`width` handshakes arriving together, each on its own connection, with their latencies.

    Concurrent, because the failure this bound exists for is a burst.
    """
    results: list[tuple[int, float]] = []
    lock = threading.Lock()

    def knock() -> None:
        started = time.perf_counter()
        response = _handshake(base)
        elapsed = time.perf_counter() - started
        with lock:
            results.append((response.status_code, elapsed))

    threads = [threading.Thread(target=knock) for _ in range(width)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results


def _median(values: list[float]) -> float:
    """The middle value, so one scheduling outlier does not decide a comparison."""
    return sorted(values)[len(values) // 2]


def test_a_full_pod_refuses_the_next_handshakes_promptly_and_counts_every_one(
    monkeypatch: pytest.MonkeyPatch, token: None, serving: Callable[..., Any]
) -> None:
    """A full pod refuses the next handshakes promptly and counts every one.

    Status is what a client acts on, latency separates a refusal from a queue, and the counter makes
    a refusing pod visible. "Promptly" is measured against admissions on a second, unbounded pod,
    since the harness itself dominates any wall clock.
    """
    monkeypatch.setenv("MCP_MAX_SESSIONS", str(PROBE_CEILING))
    bounded = _probe_server("ceiling")
    monkeypatch.setenv("MCP_MAX_SESSIONS", "0")
    unbounded = _probe_server("ceiling-baseline")
    # Both apps are built while the variable holds the value each is meant to run under: the
    # ceiling is read once, when `connector_app` wraps the server.
    monkeypatch.setenv("MCP_MAX_SESSIONS", str(PROBE_CEILING))
    bounded_app = connector_app(bounded, name="ceiling", token_env=TOKEN_ENV)
    monkeypatch.setenv("MCP_MAX_SESSIONS", "0")
    baseline_app = connector_app(unbounded, name="ceiling-baseline", token_env=TOKEN_ENV)

    with serving(bounded_app) as base, serving(baseline_app) as baseline:
        for _ in range(PROBE_CEILING):
            _open_session(base)
        before = SESSIONS_REFUSED.labels("ceiling")._value.get()
        refused = _burst(base, PROBE_BURST)
        admitted = _burst(baseline, PROBE_BURST)
        # Read inside the block: the session manager clears `_server_instances` when its `run()`
        # context exits, so this count is 0 the moment the server stops — which is a true fact
        # about a stopped pod and no evidence at all about the ceiling.
        live = len(bounded.session_manager._server_instances)

    assert [status for status, _ in refused] == [AT_CAPACITY_STATUS] * PROBE_BURST, (
        f"a pod holding {PROBE_CEILING} sessions admitted one past its ceiling: {refused}"
    )
    assert [status for status, _ in admitted] == [200] * PROBE_BURST, (
        "the baseline pod refused a handshake, so it is not a baseline"
    )
    refusal = _median([elapsed for _, elapsed in refused])
    admission = _median([elapsed for _, elapsed in admitted])
    assert refusal <= admission * MAX_REFUSAL_OVER_ADMISSION, (
        f"a refused handshake took {refusal * 1000:.1f} ms against {admission * 1000:.1f} ms for "
        "an admitted one through the same harness; a refusal does no work, so anything slower "
        "than the admission it replaced means it waited for something"
    )
    after = SESSIONS_REFUSED.labels("ceiling")._value.get()
    assert after - before == PROBE_BURST
    assert live == PROBE_CEILING, (
        "a refused handshake minted a session anyway, which makes the ceiling a log line"
    )


def test_the_refusal_says_which_bound_it_hit_and_that_it_is_worth_retrying(
    full_pod: tuple[str, FastMCP],
) -> None:
    """503 plus `Retry-After`, and a body that is not upstream's "session not found".

    404 already means unknown session and 200 means served, so the status alone is unambiguous.
    """
    base, _ = full_pod
    response = _handshake(base)
    assert response.status_code == AT_CAPACITY_STATUS
    message = response.json()["error"]["message"]
    assert str(PROBE_CEILING) in message
    assert "MCP_MAX_SESSIONS" in message
    # Not upstream's code for a stale session id: a full pod and a bad session id need different
    # responses from a client, and re-using -32600 would hide one inside the other.
    assert response.json()["error"]["code"] == -32000


def test_a_full_pod_still_serves_the_sessions_it_already_has(
    full_pod: tuple[str, FastMCP],
) -> None:
    """The bound is on new sessions; an established one keeps working while the pod is full.

    Without this the ceiling would convert a memory bound into an outage — every caller already
    mid-turn would start failing the moment the pod filled, which is strictly worse than the leak.
    """
    base, server = full_pod
    established = _server_ids(server)[0]
    assert _handshake(base).status_code == AT_CAPACITY_STATUS, "the pod was not full"
    answered = httpx.post(
        f"{base}/mcp",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "mcp-session-id": established,
        },
        json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
        timeout=10.0,
    )
    assert answered.status_code == 200, answered.text


def _server_ids(server: FastMCP) -> list[str]:
    """The session ids this server is holding, read off the manager's own map."""
    return list(server.session_manager._server_instances)


def test_a_session_that_says_goodbye_frees_its_slot(full_pod: tuple[str, FastMCP]) -> None:
    """A session that says goodbye frees its slot, through the ceiling's sweep.

    Upstream's cleanup skips a terminated transport, so without `_drop_terminated_sessions` before
    the count a pod would refuse at a ceiling of dead sessions.
    """
    base, server = full_pod
    assert _handshake(base).status_code == AT_CAPACITY_STATUS
    doomed = _server_ids(server)[0]
    deleted = httpx.delete(
        f"{base}/mcp",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "mcp-session-id": doomed,
        },
        timeout=10.0,
    )
    assert deleted.status_code in (200, 204), deleted.text
    assert _handshake(base).status_code == 200, "the freed slot was not given to the next caller"


async def test_the_ceiling_reclaims_goodbyes_even_with_the_idle_reaper_turned_off(
    monkeypatch: pytest.MonkeyPatch, token: None, serving: Callable[..., Any]
) -> None:
    """The ceiling reclaims goodbyes even with the idle reaper turned off.

    `MCP_SESSION_IDLE_TIMEOUT_SECONDS=0` disables the reaper and its reclaim sweep, so the ceiling's
    own sweep is the only one left; without it the pod would refuse everyone permanently.
    """
    monkeypatch.setenv("MCP_SESSION_IDLE_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("MCP_MAX_SESSIONS", str(PROBE_CEILING))
    server = _probe_server("no-reaper")
    app = connector_app(server, name="no-reaper", token_env=TOKEN_ENV)
    with serving(app) as base:
        opened = [_open_session(base) for _ in range(PROBE_CEILING)]
        assert _handshake(base).status_code == AT_CAPACITY_STATUS, "the pod was not full"
        for session_id in opened:
            # ASYNC210: uvicorn runs in its own thread with its own loop (see `running_server`),
            # so a blocking call here cannot stall the server being driven.
            deleted = httpx.delete(  # noqa: ASYNC210
                f"{base}/mcp",
                headers={
                    "Authorization": f"Bearer {TOKEN}",
                    "MCP-Protocol-Version": PROTOCOL_VERSION,
                    "mcp-session-id": session_id,
                },
                timeout=10.0,
            )
            assert deleted.status_code in (200, 204), deleted.text
        assert _handshake(base).status_code == 200, (
            "every session on this pod said goodbye and the next handshake was still refused, so "
            "the ceiling is counting terminated transports nothing reclaims"
        )


async def test_a_refused_request_leaves_no_session_behind_even_with_the_idle_reaper_off(
    monkeypatch: pytest.MonkeyPatch, token: None, serving: Callable[..., Any]
) -> None:
    """A refused request leaves no session behind, even with the idle reaper off.

    Upstream mints a session whenever the session-id header is absent, so a bare `DELETE /mcp` is
    answered 400 with a registered, unusable, non-terminated session behind it.
    `_settle_a_minted_session` must discard it in every configuration, or cheap requests fill the
    ceiling for the life of the process while the probes stay green.
    """
    monkeypatch.setenv("MCP_SESSION_IDLE_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("MCP_MAX_SESSIONS", str(PROBE_CEILING))
    server = _probe_server("refused-mints")
    app = connector_app(server, name="refused-mints", token_env=TOKEN_ENV)
    with serving(app) as base:
        for attempt in range(PROBE_CEILING * 2):
            # ASYNC210: uvicorn runs in its own thread with its own loop (see `running_server`), so
            # a blocking call here cannot stall the server being driven.
            refused = httpx.delete(  # noqa: ASYNC210
                f"{base}/mcp",
                headers={
                    "Authorization": f"Bearer {TOKEN}",
                    "MCP-Protocol-Version": PROTOCOL_VERSION,
                },
                timeout=10.0,
            )
            assert refused.status_code >= 400, (
                f"a bare DELETE with no session id was served {refused.status_code}; this probe "
                "depends on it being refused"
            )
            assert refused.headers.get("mcp-session-id") is not None, (
                "upstream no longer names the session it minted on the refusal, so the discard has "
                "nothing exact to act on — see `_discard_an_unusable_session`"
            )
            live = len(server.session_manager._server_instances)
            assert live == 0, (
                f"{attempt + 1} refused requests have left {live} unusable session(s) registered; "
                f"at {PROBE_CEILING} of them this pod refuses every real handshake for the life of "
                "the process, and /healthz and /livez both answer 200"
            )

        assert _handshake(base).status_code == 200, (
            "after twice the ceiling in refused requests the pod would not admit a real handshake"
        )


def test_with_the_ceiling_off_the_pod_admits_past_it(
    monkeypatch: pytest.MonkeyPatch, token: None, serving: Callable[..., Any]
) -> None:
    """`MCP_MAX_SESSIONS=0` restores the unbounded behaviour, which is what makes it a knob.

    It is also the counterfactual for every assertion above: without it, a suite in which the
    ceiling silently did nothing would look exactly like this one.
    """
    monkeypatch.setenv("MCP_MAX_SESSIONS", "0")
    server = _probe_server("unbounded")
    app = connector_app(server, name="unbounded", token_env=TOKEN_ENV)
    with serving(app) as base:
        for _ in range(PROBE_CEILING + PROBE_BURST):
            assert _handshake(base).status_code == 200
        assert len(server.session_manager._server_instances) == PROBE_CEILING + PROBE_BURST


def test_a_session_costs_about_what_the_ceiling_was_derived_from(
    monkeypatch: pytest.MonkeyPatch, token: None, serving: Callable[..., Any]
) -> None:
    """`SESSION_COST_BYTES` is a measurement, and this is the measurement.

    Hundreds of un-deleted handshakes against a real server, RSS read from `/proc` before and after.
    The band is wide (an eighth to three times) because an allocator is not a ruler; it still
    catches a tenfold cost change that would push the default ceiling over its memory budget.
    """
    monkeypatch.setenv("MCP_MAX_SESSIONS", "0")
    server = _probe_server("cost")
    app = connector_app(server, name="cost", token_env=TOKEN_ENV)
    with serving(app) as base, httpx.Client() as client:
        for _ in range(COST_PROBE_WARMUP):
            _open_session(base, client)
        time.sleep(0.3)
        before = _rss_bytes()
        for _ in range(COST_PROBE_SESSIONS):
            _open_session(base, client)
        time.sleep(0.3)
        growth = (_rss_bytes() - before) / COST_PROBE_SESSIONS
        print(f"MEASURED per-session growth: {growth:.0f} B over {COST_PROBE_SESSIONS} sessions")
        assert len(server.session_manager._server_instances) == (
            COST_PROBE_WARMUP + COST_PROBE_SESSIONS
        )
    assert SESSION_COST_BYTES / 8 <= growth <= SESSION_COST_BYTES * 3, (
        f"a session measured {growth:.0f} B against the {SESSION_COST_BYTES} B the shipped "
        f"ceiling of {DEFAULT_MAX_SESSIONS} was derived from; the default is now "
        f"{DEFAULT_MAX_SESSIONS * growth / 1024 / 1024:.0f} MB of a "
        f"{SESSION_BACKLOG_BUDGET_BYTES / 1024 / 1024:.0f} MB budget"
    )


def _simultaneous_burst(base: str, width: int) -> list[int]:
    """`width` handshakes released from one barrier, so they are in flight together.

    Unlike `_burst`, every request leaves before the server has answered any, the only arrival
    pattern that can catch an admission decision taken before the mint.
    """
    gate = threading.Barrier(width)
    statuses: list[int] = []
    lock = threading.Lock()

    def knock() -> None:
        gate.wait(timeout=60)
        response = _handshake(base)
        with lock:
            statuses.append(response.status_code)

    threads = [threading.Thread(target=knock) for _ in range(width)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return statuses


def test_a_simultaneous_burst_against_an_empty_pod_cannot_admit_past_the_ceiling(
    monkeypatch: pytest.MonkeyPatch, token: None, serving: Callable[..., Any]
) -> None:
    """A simultaneous burst against an empty pod cannot admit past the ceiling.

    Upstream registers a session several awaits after the ASGI entry, so a check on entry would let
    a whole burst see the pre-burst count. Bursting against an empty pod opens that window. Asserted
    on the live count as well as the statuses.
    """
    monkeypatch.setenv("MCP_MAX_SESSIONS", str(PROBE_CEILING))
    server = _probe_server("burst")
    app = connector_app(server, name="burst", token_env=TOKEN_ENV)
    with serving(app) as base:
        statuses = _simultaneous_burst(base, BURST_WIDTH)
        live = len(server.session_manager._server_instances)

    admitted = statuses.count(200)
    assert len(statuses) == BURST_WIDTH, "a thread in the burst never answered"
    assert admitted == PROBE_CEILING, (
        f"{BURST_WIDTH} handshakes arriving together against an empty pod with a ceiling of "
        f"{PROBE_CEILING} were admitted {admitted} times; a ceiling read before upstream mints is "
        "a ceiling every concurrent caller walks past"
    )
    assert statuses.count(AT_CAPACITY_STATUS) == BURST_WIDTH - PROBE_CEILING
    assert live == PROBE_CEILING, (
        f"the pod is holding {live} sessions against a ceiling of {PROBE_CEILING}"
    )


def test_the_refusals_status_and_retry_interval_are_what_a_client_is_told(
    full_pod: tuple[str, FastMCP],
) -> None:
    """The refusal's status and `Retry-After` are pinned as literals, not their own constants.

    They are a contract with a caller this repository does not own. `Retry-After` is 10 s because
    held slots refund no sooner than the unused-session timeout, and a 1 s retry would make
    displaced callers cost the pod real CPU.
    """
    base, _ = full_pod
    response = _handshake(base)
    assert response.status_code == 503
    assert AT_CAPACITY_STATUS == 503
    assert response.headers.get("Retry-After") == "10"
    assert AT_CAPACITY_RETRY_AFTER_SECONDS == 10


def test_a_pod_that_is_refusing_says_so_once_an_interval_and_not_once_a_refusal(
    full_pod: tuple[str, FastMCP], caplog: pytest.LogCaptureFixture
) -> None:
    """A refusing pod logs once per interval with a count, not once per refusal.

    A full pod is refused by every displaced caller; a warning each would bury the lines explaining
    why. The counter is the exact per-refusal channel.
    """
    base, _ = full_pod
    caplog.clear()
    with caplog.at_level("WARNING", logger="mcp_server_kit.sessions"):
        refusals = _burst(base, PROBE_BURST)
    assert [status for status, _ in refusals] == [AT_CAPACITY_STATUS] * PROBE_BURST
    lines = [record for record in caplog.records if record.name == "mcp_server_kit.sessions"]
    assert len(lines) == 1, (
        f"{PROBE_BURST} refusals inside one {REFUSAL_LOG_INTERVAL_SECONDS:.0f} s window produced "
        f"{len(lines)} warning lines"
    )
    assert "chemclaw_mcp_sessions_refused_total" in lines[0].getMessage(), (
        "the one line printed does not point at the channel that does carry every refusal: "
        f"{lines[0].getMessage()}"
    )
