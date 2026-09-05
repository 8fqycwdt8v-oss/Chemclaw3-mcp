"""`connector_app` as a caller meets it: a real socket, a real MCP session, a real refusal.

Every other test in this package drives a function or an in-process ASGI app. Three of the
behaviours `connector_app` promises cannot be seen that way, and each shipped broken *because* the
only tests were the in-process ones:

- **The body cap's counting half.** Its one test sent `content=b"x" * 4096`, which httpx sends with
  a `Content-Length` — so it exercised the declared pre-check and never the counter. Over a real
  socket, with the two `BaseHTTPMiddleware` layers `connector_app` installs in between, the
  counter's signal arrived at the cap as a nested `ExceptionGroup` and became a 500 with a
  per-request traceback. The class's own docstring says it was fixed once for exactly this — "the
  test for it passed for the wrong reason" — which is why the replacement drives a chunked upload
  through the whole stack rather than the middleware alone.
- **The error sanitiser's exemption.** It is written against `ToolError.__cause__`, a property of
  the *upstream* tool manager, and the only way to know which of upstream's `ToolError`s carry a
  cause is to make upstream raise them.
- **What `/metrics` actually publishes.** The claim is about the exposition, not about the code
  that generates it.

The probe capability is deliberately tiny: this file is about the shape every server shares, not
about any server's chemistry.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP
from mcp_server_kit import Dataset
from mcp_server_kit.app import connector_app

TOKEN = "test-token-for-the-kit"
TOKEN_ENV = "MCP_KIT_PROBE_TOKEN"
# Small enough that an oversize body is cheap to send, large enough that a real MCP handshake and
# tool call fit under it comfortably.
MAX_BYTES = 8_192

# What one real call supplies, written out so the exposition can be asked about these exact strings
# rather than about the words that happen to name them today. Every one is something a caller
# controls: three identity headers and a tool argument.
ACTOR = "alice@example.test"
SESSION = "session-7f3a91c4"
CORRELATION = "4bf92f3577b34da6a3ce929d0e0e4736"
CALLER_ARGUMENT = "CC(=O)Oc1ccccc1C(=O)O"


def _probe_server() -> FastMCP:
    """A FastMCP carrying one healthy tool and two that fail in the two ways that differ."""
    server = FastMCP("probe")

    @server.tool()
    def echo(text: str) -> str:
        """Return what was passed in."""
        return text

    @server.tool()
    def boom_internal() -> str:
        """Raise the kind of fault whose text must never reach the model."""
        raise RuntimeError("PGPASSWORD=hunter2 at postgres.internal:5432")

    @server.tool()
    def boom_domain() -> str:
        """Raise the kind of refusal that is the whole content of the answer."""
        raise ValueError("unknown solvent 'unobtainium'; see the vendored solvent table")

    @server.tool()
    def boom_domain_with_secret() -> str:
        """A caller-safe `ValueError` whose text happens to carry a secret — e.g. a validation

        message quoting an input that was a connection string. `UnicodeDecodeError`,
        `JSONDecodeError` and pydantic `ValidationError` are all `ValueError`s and routinely echo
        the offending input, so the caller-safe branch must redact.
        """
        raise ValueError("could not parse config PGPASSWORD=hunter2 at postgres.internal:5432")

    return server


def _free_port() -> int:
    """An ephemeral loopback port, released immediately for uvicorn to claim."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def running_server() -> Iterator[str]:
    """The probe capability under uvicorn on loopback, wrapped by the real `connector_app`."""
    import os

    os.environ[TOKEN_ENV] = TOKEN
    app = connector_app(
        _probe_server(), name="probe", token_env=TOKEN_ENV, max_request_bytes=MAX_BYTES
    )
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/healthz", timeout=1.0).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:  # pragma: no cover - only reached if the app never becomes ready
        pytest.fail("the probe server did not become ready within 30 s")
    yield base
    server.should_exit = True
    thread.join(timeout=10)


@asynccontextmanager
async def _session(
    base: str, *, identity: dict[str, str] | None = None
) -> AsyncIterator[ClientSession]:
    """An initialised MCP session against the running server, carrying the bearer token.

    `identity` adds the `X-Chemclaw-*` headers a real call from Chemclaw3 carries, so a test can
    ask what happened to those *values* rather than to the words that name them.
    """
    headers = {"Authorization": f"Bearer {TOKEN}", **(identity or {})}
    async with (
        httpx.AsyncClient(headers=headers) as http_client,
        streamable_http_client(f"{base}/mcp", http_client=http_client) as (rx, tx, _),
        ClientSession(rx, tx) as session,
    ):
        await session.initialize()
        yield session


@contextmanager
def _serving(app: FastAPI) -> Iterator[str]:
    """Run one `connector_app` under uvicorn on a free loopback port for one test's duration.

    Distinct from the `running_server` fixture above: that one is module-scoped and waits for
    `/healthz` to answer **200** specifically, which a failing `readiness` callable never does.
    This waits for the port to accept a connection at all — uvicorn's lifespan has then completed,
    whatever `/healthz` itself reports — because a readiness probe answering 503 is a successful
    HTTP response, not a startup failure.
    """
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{base}/healthz", timeout=1.0)
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:  # pragma: no cover - only reached if the app never accepts a connection
        pytest.fail("the server did not accept a connection within 30 s")
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_readiness_failure_is_a_503_naming_the_reason() -> None:
    """The failure mode `readiness` exists to catch, exercised over the real transport.

    Before `readiness` existed, `/healthz` was a constant 200 in every server — evidence the
    session manager was running and nothing about whether the server could answer, which is
    precisely how a `chem` pod with a corpus that failed its checksum passed the probe, took
    traffic, and failed every tool call (see `mcp_server_kit.app.connector_app`'s docstring).
    That mechanism shipped with no test anywhere in this fleet driving a failing callable through
    a live `/healthz` — every server's own test only exercises the success path. This is that
    test, once, at the one place every server's readiness check is the same code.
    """

    def _broken() -> list[Dataset]:
        raise RuntimeError("PGPASSWORD=hunter2 could not verify the solvent table")

    app = connector_app(_probe_server(), name="probe-unready", readiness=_broken)
    with _serving(app) as base:
        response = httpx.get(f"{base}/healthz", timeout=5.0)
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "unready"
        assert body["server"] == "probe-unready"
        assert "could not verify the solvent table" in body["reason"]
        # `/healthz` carries no bearer check, so this reason reaches anything that can open a
        # socket to the pod — unlike a tool fault, which `_sanitize_tool_errors` never lets carry
        # its raw text past a `ValueError`. The secret this exception happens to name must never
        # reach an unauthenticated caller here, whatever the readiness callable raised.
        assert "hunter2" not in response.text
        assert "PGPASSWORD=***" in body["reason"]

        exposition = httpx.get(f"{base}/metrics", timeout=5.0).text
        assert 'chemclaw_mcp_ready{server="probe-unready"} 0.0' in exposition


def test_readiness_success_names_the_verified_datasets() -> None:
    """The companion path: a `readiness` that succeeds is what `/healthz` reports back."""
    verified = Dataset(
        name="solvent-table",
        version="3",
        licence="CC0",
        retrieved_from="internal",
        description="probe",
        sha256="0" * 64,
        records_path=Path("/dev/null"),
    )

    app = connector_app(_probe_server(), name="probe-ready", readiness=lambda: [verified])
    with _serving(app) as base:
        response = httpx.get(f"{base}/healthz", timeout=5.0)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["datasets"] == ["solvent-table@3"]

        exposition = httpx.get(f"{base}/metrics", timeout=5.0).text
        assert 'chemclaw_mcp_ready{server="probe-ready"} 1.0' in exposition


def test_a_declared_oversize_body_is_refused(running_server: str) -> None:
    """The half that already worked: a `content-length` over the cap never reaches a handler."""
    response = httpx.post(
        f"{running_server}/mcp",
        headers={"authorization": f"Bearer {TOKEN}"},
        content=b"x" * (MAX_BYTES * 2),
        timeout=10.0,
    )
    assert response.status_code == 413
    assert response.text == "request body too large"


def test_a_chunked_oversize_body_is_refused_with_413_and_not_a_500(running_server: str) -> None:
    """The half that never worked, and the reason the cap has a counter at all.

    A chunked upload declares no `content-length`, so the running total is the only thing that
    bounds it. Measured before the fix, against this exact stack: **500 Internal Server Error**
    plus a ~40-line nested `ExceptionGroup` traceback per request — so anything that could reach
    the pod could turn a size refusal into unbounded log volume, and an operator's dashboard
    showed unhandled server errors rather than rejected oversize requests.

    Sent with a body iterator so httpx uses `Transfer-Encoding: chunked`; asserted as an absence
    of 500 as well as a presence of 413, because "some error happened" is what it did before.
    """

    def chunks() -> Iterator[bytes]:
        for _ in range(8):
            yield b"x" * MAX_BYTES

    response = httpx.post(
        f"{running_server}/mcp",
        headers={"authorization": f"Bearer {TOKEN}"},
        content=chunks(),
        timeout=10.0,
    )
    assert response.status_code == 413, f"chunked oversize answered {response.status_code}"
    assert response.text == "request body too large"


def test_a_body_under_the_cap_is_served_chunked_too(running_server: str) -> None:
    """The cap must not refuse a legitimate streamed request — otherwise 413 proves nothing."""

    def chunks() -> Iterator[bytes]:
        yield b'{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}'

    response = httpx.post(
        f"{running_server}/mcp",
        headers={
            "authorization": f"Bearer {TOKEN}",
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
        },
        content=chunks(),
        timeout=10.0,
    )
    assert response.status_code != 413


async def test_an_unknown_tool_name_is_named_back_to_the_caller(running_server: str) -> None:
    """Upstream's `Unknown tool: x` is a caller-safe message and must survive the sanitiser.

    `ToolManager.call_tool` raises it with no `from`, so it reached the sanitiser looking exactly
    like an internal fault: the model was told "an internal error occurred" — which gives it
    nothing to correct — and `logger.exception` fired at ERROR with a stack trace for what is a
    client input error, diluting the signal the sanitiser exists to preserve.

    This is the repository's own "refuse rather than approximate" rule applied to the transport:
    an unknown solvent is an error naming the corpus, so an unknown tool is an error naming the
    tool.
    """
    async with _session(running_server) as session:
        result = await session.call_tool("no_such_tool", {})
        assert result.isError is True
        assert "no_such_tool" in str(result.content)


async def test_an_internal_fault_is_still_replaced(running_server: str) -> None:
    """The exemption must not widen: a `RuntimeError`'s text still never reaches the model."""
    async with _session(running_server) as session:
        result = await session.call_tool("boom_internal", {})
        assert result.isError is True
        assert "an internal error occurred" in str(result.content)
        assert "PGPASSWORD" not in str(result.content)


async def test_a_domain_refusal_still_passes_through(running_server: str) -> None:
    """And a deliberately worded `ValueError` is still the whole content of the answer."""
    async with _session(running_server) as session:
        result = await session.call_tool("boom_domain", {})
        assert result.isError is True
        assert "unobtainium" in str(result.content)


async def test_a_caller_safe_message_is_redacted_before_the_model_sees_it(
    running_server: str,
) -> None:
    """A `ValueError` passes through, but not with a secret in it.

    The caller-safe branch used to re-raise verbatim, so a validation message quoting a connection
    string reached the model unredacted — the 503 readiness path redacted, this one did not. The
    redaction is the same `redact_secrets` both paths share, applied so the family of `ValueError`
    subclasses that echo their input (pydantic `ValidationError`, `UnicodeDecodeError`,
    `JSONDecodeError`) cannot leak a mounted secret. The surrounding worded text still passes.
    """
    async with _session(running_server) as session:
        result = await session.call_tool("boom_domain_with_secret", {})
        assert result.isError is True
        content = str(result.content)
        assert "hunter2" not in content
        assert "PGPASSWORD=***" in content
        # The message is still the worded refusal, not the generic internal-error notice.
        assert "an internal error occurred" not in content
        assert "could not parse config" in content


def test_upstream_still_chains_a_tool_fault_and_still_does_not_chain_unknown_tool() -> None:
    """The property the sanitiser's exemption reads, asserted against the installed `mcp`.

    `_sanitize_tool_errors` distinguishes a fault from a refusal by `ToolError.__cause__`: upstream
    raises `ToolError(...) from e` when a tool body fails and raises a bare `ToolError` for a name
    it does not have. Neither is documented as a promise, so this pins both — an upstream release
    that starts chaining `Unknown tool` turns the exemption red here rather than letting a fault's
    text reach the model.
    """
    import inspect

    from mcp.server.fastmcp.tools import base, tool_manager

    assert 'raise ToolError(f"Error executing tool {self.name}: {e}") from e' in inspect.getsource(
        base.Tool.run
    )
    source = inspect.getsource(tool_manager.ToolManager.call_tool)
    assert 'raise ToolError(f"Unknown tool: {name}")' in source
    assert "from" not in source.split("Unknown tool")[1].split("\n")[0]


async def test_metrics_is_open_and_carries_no_identity(running_server: str) -> None:
    """`/metrics` is unauthenticated on purpose, so what it may carry is the whole control.

    It is *not* "counts only" and never was: `generate_latest(REGISTRY)` on the default registry
    publishes `python_info`, `process_start_time_seconds`, `process_open_fds` and the rest. That is
    fine — an operator wants them, and the NetworkPolicy admits only the agent's pods and the
    monitoring namespace — but the sentence a reviewer relies on when deciding this endpoint may
    stay open has to be the true one, and the true one is: no request content, no caller identity,
    no tool argument.

    **The assertion is about the values, and it used to be about six words.** Scanning for `actor`,
    `session_id` and `correlation` catches only a developer who names the new label the way the
    docstring does — `principal="alice@example.com"` and `cid="4bf92f35..."` passed every one of
    those assertions, and the bare word `session` and the dry-run header were not in the list at
    all. So a real call carries real identity through the whole stack first, and what is asserted
    absent is the strings that call sent: nothing a caller supplies may name a series, whatever the
    label is called.
    """
    identity = {
        "X-Chemclaw-Actor": ACTOR,
        "X-Chemclaw-Session": SESSION,
        "X-Chemclaw-Correlation-Id": CORRELATION,
        "X-Chemclaw-Dry-Run": "true",
    }
    async with _session(running_server, identity=identity) as session:
        await session.call_tool("echo", {"text": CALLER_ARGUMENT})

    response = httpx.get(f"{running_server}/metrics", timeout=5.0)
    assert response.status_code == 200
    exposition = response.text
    for supplied in (ACTOR, SESSION, CORRELATION, CALLER_ARGUMENT, TOKEN):
        assert supplied not in exposition, (
            f"/metrics is unauthenticated and published {supplied!r}, which the caller supplied; a "
            "labelled metric on this endpoint must never take an actor, a session, a correlation "
            "id or a tool argument as a label, whatever that label is named"
        )
    lowered = exposition.lower()
    for forbidden in ("actor", "session_id", "correlation", "smiles", "authorization", "bearer"):
        assert forbidden not in lowered, (
            f"/metrics is unauthenticated and published {forbidden!r}; a labelled metric on this "
            "endpoint must never carry an actor, a session, a correlation id or a tool argument"
        )


def test_the_egress_counter_is_unlabelled(running_server: str) -> None:
    """The destination host of a refused connection is attacker-influenced, so it is not a label.

    `metrics.py` states it, `CLAUDE.md` says this file asserts it, and until now this file did not
    — a bare word in a prose document is exactly the shape of claim this repository's own rules
    say to check. A label here would be unbounded by construction: anything that can make the pod
    try to resolve a name it chose would mint a series.
    """
    exposition = httpx.get(f"{running_server}/metrics", timeout=5.0).text
    lines = [
        line
        for line in exposition.splitlines()
        if line.startswith("chemclaw_mcp_egress_refused_total")
    ]
    assert lines, "the egress counter is not published at all"
    for line in lines:
        assert "{" not in line, (
            f"chemclaw_mcp_egress_refused_total carries a label ({line!r}); the only thing there "
            "is to label it with is the destination host, which a caller influences and nothing "
            "bounds"
        )


async def test_metrics_publishes_what_a_tool_call_did(running_server: str) -> None:
    """The other direction, and the one the absence test above cannot give: it is not empty.

    Measured before this instrumentation existed: **ten** series on a running server, all ten
    `prometheus_client` built-ins. An operator could read the interpreter version and the pod's
    open file descriptors and could not answer "which tool is slow", "which tool is failing", or
    "is anything being called". The absence assertion above was fully satisfied by that, which is
    exactly why it needs a companion — a `/metrics` that publishes nothing passes every rule about
    what it must not publish.

    All three outcomes are driven through the real transport rather than asserted off the code,
    because `outcome` is decided by `ToolError.__cause__` — a property of the *upstream* tool
    manager, and the same one `_sanitize_tool_errors`'s exemption reads.
    """
    async with _session(running_server) as session:
        await session.call_tool("echo", {"text": "hello"})
        await session.call_tool("boom_domain", {})
        await session.call_tool("boom_internal", {})

    exposition = httpx.get(f"{running_server}/metrics", timeout=5.0).text
    for expected in (
        'chemclaw_mcp_tool_calls_total{outcome="ok",server="probe",tool="echo"}',
        'chemclaw_mcp_tool_calls_total{outcome="refused",server="probe",tool="boom_domain"}',
        'chemclaw_mcp_tool_calls_total{outcome="failed",server="probe",tool="boom_internal"}',
        'chemclaw_mcp_tool_duration_seconds_count{server="probe",tool="echo"}',
        'chemclaw_mcp_requests_total{path="/mcp",server="probe",status="200"}',
        "chemclaw_mcp_build_info{revision=",
        "chemclaw_mcp_egress_guard_armed 1.0",
    ):
        assert expected in exposition, f"/metrics does not publish {expected!r}"


async def test_an_unknown_tool_name_cannot_mint_a_metric_series(running_server: str) -> None:
    """The trap that makes this metric safe, and it is not safe by construction.

    A tool name is not an actor, a session or an argument, so it is allowed as a label — but it is
    **caller-supplied**: `ToolManager.call_tool` raises `Unknown tool: <whatever>` for anything it
    does not have, so an unclamped counter mints one Prometheus series per string a confused model
    or a hostile caller sends, in the pod's memory, unbounded. Measured in the audit's prototype: a
    probe calling `nope` minted `tool="nope"`.

    So the call is still counted — a caller guessing tool names is a real signal — and it is
    counted under the fixed `<unknown>` sentinel.
    """
    async with _session(running_server) as session:
        result = await session.call_tool("definitely_not_a_tool_here", {})
        assert result.isError is True

    exposition = httpx.get(f"{running_server}/metrics", timeout=5.0).text
    assert "definitely_not_a_tool_here" not in exposition, (
        "a caller-supplied tool name reached a metric label; the label set is then unbounded and "
        "anything that can reach the pod can grow it until the process runs out of memory"
    )
    assert 'chemclaw_mcp_tool_calls_total{outcome="refused",server="probe",tool="<unknown>"}' in (
        exposition
    ), "an unknown tool name must still be counted, under the sentinel"


def test_metrics_counts_the_requests_it_refused(running_server: str) -> None:
    """A counter whose help says "HTTP requests served" must see the ones nothing served.

    `chemclaw_mcp_requests_total` was booked inside `CallerLogMiddleware`, and `connector_app` adds
    the credential check and the body cap *after* it — so Starlette's add-order put both outside
    the counter, and both short-circuit. Measured against this stack: three 401s and two 413s
    produced **zero** series between them, which makes `rate(...{status=~"4.."})` permanently 0 —
    the one expression an operator writes to see a fleet-wide credential or payload problem.

    Both refusals are driven over a real socket, because both happen in ASGI layers a
    `TestClient`-less in-process call never reaches.
    """
    for _ in range(3):
        refused = httpx.post(f"{running_server}/mcp", content=b"{}", timeout=10.0)
        assert refused.status_code == 401
    oversize = httpx.post(
        f"{running_server}/mcp",
        headers={"authorization": f"Bearer {TOKEN}"},
        content=b"x" * (MAX_BYTES * 2),
        timeout=10.0,
    )
    assert oversize.status_code == 413

    exposition = httpx.get(f"{running_server}/metrics", timeout=5.0).text
    for expected in (
        'chemclaw_mcp_requests_total{path="/mcp",server="probe",status="401"}',
        'chemclaw_mcp_requests_total{path="/mcp",server="probe",status="413"}',
    ):
        assert expected in exposition, (
            f"/metrics does not publish {expected!r}; the request counter cannot see the requests "
            "the layers outside it refuse"
        )


def test_connector_app_refuses_to_wrap_one_capability_twice() -> None:
    """Wrapping is not idempotent, so a second call must be an error rather than a double count.

    Each of the four behaviours is a wrapper that captures the previous `call_tool`; a second call
    stacks a second set. Measured on a twice-wrapped server: one `tools/call` booked
    `chemclaw_mcp_tool_calls_total 2.0`, and nothing at the call site said so.
    """
    server = _probe_server()
    connector_app(server, name="twice-probe")
    with pytest.raises(RuntimeError, match="already wrapped"):
        connector_app(server, name="twice-probe")


def test_a_probe_path_with_a_trailing_slash_answers(running_server: str) -> None:
    """`/healthz/` and `/metrics/` must *answer*, not merely escape the credential check.

    `auth._is_open` normalises the trailing slash so a kubelet probe configured as
    `path: /healthz/` is not refused — and that was only half of it. FastAPI matches routes
    exactly, `connector_app` mounts the MCP transport at `/`, and a mount swallows the redirect
    Starlette would otherwise issue: measured against every server in this fleet under real
    uvicorn, `GET /healthz/` answered **404**. The middleware-level test for the same behaviour
    asserted `status_code != 401`, which a 404 satisfies, so nothing could tell the fix working
    from the fix doing nothing.

    Redirects are not followed here deliberately. A 3xx is what kubelet counts as a *successful*
    probe, so a redirect would be indistinguishable from a served answer at the one moment the
    difference matters — see the unreadiness test below.
    """
    healthz = httpx.get(f"{running_server}/healthz/", timeout=5.0, follow_redirects=False)
    assert healthz.status_code == 200, "a trailing-slash probe path must answer the probe itself"
    assert healthz.json()["status"] == "ok"
    assert healthz.json()["server"] == "probe"

    metrics = httpx.get(f"{running_server}/metrics/", timeout=5.0, follow_redirects=False)
    assert metrics.status_code == 200
    assert "chemclaw_mcp_build_info" in metrics.text


def test_a_trailing_slash_probe_still_reports_unreadiness() -> None:
    """The alias serves the route rather than pointing at it, which is why a 503 survives.

    This is the whole argument for an alias over a redirect: kubelet treats any 2xx **or 3xx** as
    a passing probe, so a pod whose corpus failed its checksum would have been reported ready by
    a `/healthz/` probe that got a 307 — the exact failure `readiness` exists to end, restored by
    the fix for it.
    """

    def _broken() -> list[Dataset]:
        raise RuntimeError("could not verify the solvent table")

    app = connector_app(_probe_server(), name="probe-unready-slash", readiness=_broken)
    with _serving(app) as base:
        response = httpx.get(f"{base}/healthz/", timeout=5.0, follow_redirects=False)
        assert response.status_code == 503
        assert response.json()["status"] == "unready"
