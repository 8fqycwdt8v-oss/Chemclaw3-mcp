"""How many MCP sessions this pod will hold, and what it does with the handshake that arrives full.

`test_sessions.py` covers the *reaper* — how long one session lives. This covers the other half of
the same resource: how many exist at once. They are different controls and neither implies the
other, which is why nothing here reads the idle timeout and nothing there reads the ceiling.

The gap these tests close: upstream's `_server_instances` is a plain dict with no admission at all,
a session is 56.6 kB that is refunded no earlier than `MCP_SESSION_IDLE_TIMEOUT_SECONDS` later, and
one authenticated client on loopback opens 261 of them a second. An idle timeout cannot see that —
nothing has been idle long enough to reap — so the pod reaches its memory limit and is OOMKilled,
taking every session on it down together.

Four properties, each with its own failure:

- **The refusal is prompt.** A full pod turns a handshake away before it mints anything, rather
  than holding it until a session frees up. A queued handshake returns after the caller's own
  timeout, which is the failure `servers/calc/engine/admission.py` argues at length one layer down.
- **The refusal mints nothing.** If the count still went up, the ceiling would be a log line.
- **A full pod still serves the sessions it has.** The bound is on *new* sessions; refusing a tool
  call on an established session would turn a memory bound into an outage.
- **A session costs what the ceiling was derived from.** The default is 1,024 because a session is
  56.6 kB and an eighth of the smallest pod's 512Mi is the budget. If a session started costing ten
  times that, the derivation would be wrong and nothing else in this file would notice.
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
    AT_CAPACITY_STATUS,
    DEFAULT_MAX_SESSIONS,
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

#: How much slower than an *admitted* handshake a refused one may be. A ratio rather than a wall
#: clock, and that is the whole point: measured, a 12-way burst of refusals took 487 ms at the
#: median and a 12-way burst of *admissions* against an unbounded pod took 552 ms — both of them
#: almost entirely the probe's own thread and connection setup. Against one shared connection,
#: where the harness is out of the way, a refusal is 0.95 ms and an admission 3.16 ms. So an
#: absolute bound here would be measuring this runner; the comparison measures the server, and it
#: is the assertion the design actually makes — a refusal does no work, so it cannot be slower than
#: the admission it replaced. The slack is for scheduling noise between two bursts, not for a queue:
#: a queued refusal waits for a session to free, which in this fleet is up to
#: `MCP_SESSION_IDLE_TIMEOUT_SECONDS`, three orders of magnitude away.
MAX_REFUSAL_OVER_ADMISSION = 2.0

#: Sessions opened by the cost probe, and the warm-up that precedes them so the allocator's own
#: first-touch growth is not charged to the curve.
COST_PROBE_SESSIONS = 400
COST_PROBE_WARMUP = 50


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

    The ceiling is read when `connector_app` builds the app, so the variable is set before that
    rather than patched afterwards — the number a gate enforces and the number it was built from
    must be the same number.
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

    `client` is threaded through for the cost probe alone, which opens hundreds: a fresh connection
    per handshake makes that probe four times slower and measures the *client's* socket churn
    alongside the server's session. Every other probe here wants an independent connection.
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
    """The ceiling is arithmetic over two measured numbers, not a figure somebody liked.

    Re-derived here rather than transcribed, so lowering `SESSION_COST_BYTES` or raising
    `DEFAULT_MAX_SESSIONS` without the other cannot pass. The budget is an eighth of the smallest
    pod's memory limit; `tests/test_fleet.py` is what checks that limit against the shipped
    Deployments, because the kit cannot see a server.
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

    Concurrent rather than sequential because the failure this bound exists for is a burst: one
    client on loopback opens 261 handshakes a second, and a ceiling that only holds against a polite
    serial caller holds against nobody.
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
    """The driven saturation probe: N held, N+1..N+12 arrive together, all refused fast.

    Three things are asserted about the same burst and they are not the same claim. The status is
    what a client acts on. The latency is what separates a refusal from a queue — a queued
    handshake comes back when a session frees, which here is up to the idle timeout. The counter is
    what makes a refusing pod visible to an operator; without it a pod at its ceiling looks
    identical to one nobody is calling.

    **"Promptly" is measured against an admission rather than against a clock**, and a second pod
    with the ceiling off is what supplies the comparison. The first draft of this test asserted a
    wall clock and passed at 487 ms per refusal — a figure that turned out to be the probe's own
    twelve threads, since twelve *admitted* handshakes through the same harness cost 552 ms. A
    bound that large would have passed a refusal that queued for half a second, which is the defect.
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

    The status is the machine-readable channel — unlike a refused *tool call*, which the protocol
    flattens into one untyped text block and which is why `servers/calc` needs a marker token. Here
    404 already means "unknown session id" and 200 means served, so a third status is unambiguous
    and the body needs to carry no convention at all.
    """
    base, _ = full_pod
    response = _handshake(base)
    assert response.status_code == AT_CAPACITY_STATUS
    assert response.headers.get("Retry-After") == "1"
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
    """The ceiling reopens, and it reopens through the sweep rather than through the reaper.

    A politely-terminated session stays in `_server_instances` — upstream's own cleanup skips a
    terminated transport — so without `_drop_terminated_sessions` running before the count, a pod
    whose callers all said goodbye would refuse at a ceiling of corpses. That is why this test is
    here and not in `test_sessions.py`: the sweep already existed, and the ceiling is what makes
    its absence fatal rather than merely wasteful.
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
    """The ceiling's own sweep, in the one configuration where nothing else is sweeping.

    `MCP_SESSION_IDLE_TIMEOUT_SECONDS=0` is a supported setting — it restores upstream's unbounded
    reaping, which a deployment would ask for only deliberately — and it turns
    `apply_session_idle_timeout` off entirely, taking `_reclaim_after_every_request` with it.
    Upstream's own cleanup skips a *terminated* transport, so with both sweeps gone a politely
    deleted session stays in `_server_instances` forever and the ceiling fills with corpses: every
    caller refused, permanently, on a pod nobody is using.

    Written because the mutation that removes the ceiling's sweep left every other test in this file
    green. The reclaim sweep runs on the shipped configuration and hid it.
    """
    monkeypatch.setenv("MCP_SESSION_IDLE_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("MCP_MAX_SESSIONS", str(PROBE_CEILING))
    server = _probe_server("no-reaper")
    app = connector_app(server, name="no-reaper", token_env=TOKEN_ENV)
    with serving(app) as base:
        opened = [_open_session(base) for _ in range(PROBE_CEILING)]
        assert _handshake(base).status_code == AT_CAPACITY_STATUS, "the pod was not full"
        for session_id in opened:
            deleted = httpx.delete(
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

    Driven rather than reasoned: 400 un-deleted handshakes against a real server under uvicorn,
    with the process's own RSS read from `/proc` before and after. The band is wide — an eighth to
    three times the constant — because an allocator is not a ruler and this runner is shared. It is
    still the assertion that matters: the default ceiling is `SESSION_BACKLOG_BUDGET_BYTES` divided
    by this constant, so a session that started costing ten times as much would put the shipped
    default 10x over the memory budget it was derived from, and every other test in this file would
    still pass.

    Measured when written, on the real `chem` app in its own process: 56.6 kB per session, flat to
    three digits at every 200-session mark from 200 to 1,000.
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
