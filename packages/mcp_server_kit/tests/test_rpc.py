"""`rpc_routes` as a backend's client meets it: a real socket, the real middleware, a real refusal.

A tiny probe capability served over both transports by `connector_app(rpc=...)`. What is held:

- the credential, the body cap and the request counter are the `/mcp` ones, because they are
  middleware, and an unset token refuses every request;
- the `X-Chemclaw-*` identity is read per request and reaches the tool body;
- an error is sanitised as on MCP (a fault is replaced and carries one `error id`, a refusal passes
  redacted), and a full pod, a refusal and a fault are three different statuses;
- the metrics carry the operation as a clamped path and nothing about the caller;
- the Host rule and the egress guard apply.
"""

from __future__ import annotations

import logging
import os
import socket
from collections.abc import Callable, Iterator
from typing import Any, ClassVar

import httpx
import pytest
from chemclaw_contracts.rpc import RpcError, WireRequest, rpc_path
from fastapi.testclient import TestClient
from mcp.server.fastmcp import FastMCP
from mcp_server_kit import current_caller
from mcp_server_kit.app import connector_app
from mcp_server_kit.limits import Admission, TimeBudgetExceeded
from prometheus_client import REGISTRY

TOKEN = "test-token-for-the-rpc-kit"
TOKEN_ENV = "MCP_KIT_RPC_PROBE_TOKEN"
SERVER = "rpc-probe"
MAX_BYTES = 8_192

ACTOR = "alice@example.test"
SESSION = "session-7f3a91c4"
CORRELATION = "4bf92f3577b34da6a3ce929d0e0e4736"


class EchoRequest(WireRequest):
    """`echo`."""

    tool_name: ClassVar[str] = "echo"
    text: str
    scale: float | None = None


class WhoamiRequest(WireRequest):
    """`whoami`: takes no arguments."""

    tool_name: ClassVar[str] = "whoami"


class FaultRequest(WireRequest):
    """`boom_internal`."""

    tool_name: ClassVar[str] = "boom_internal"


class RefusalRequest(WireRequest):
    """`boom_domain`."""

    tool_name: ClassVar[str] = "boom_domain"


class SecretRefusalRequest(WireRequest):
    """`boom_secret`."""

    tool_name: ClassVar[str] = "boom_secret"


class FullRequest(WireRequest):
    """`boom_full`."""

    tool_name: ClassVar[str] = "boom_full"


class BudgetRequest(WireRequest):
    """`boom_budget`."""

    tool_name: ClassVar[str] = "boom_budget"


class OverflowRequest(WireRequest):
    """`overflow`."""

    tool_name: ClassVar[str] = "overflow"
    x: float


class EgressRequest(WireRequest):
    """`dial_out`."""

    tool_name: ClassVar[str] = "dial_out"


MODELS: dict[str, type[WireRequest]] = {
    model.tool_name: model
    for model in (
        EchoRequest,
        WhoamiRequest,
        FaultRequest,
        RefusalRequest,
        SecretRefusalRequest,
        FullRequest,
        BudgetRequest,
        EgressRequest,
        OverflowRequest,
    )
}


def _probe_server() -> FastMCP:
    """A FastMCP whose tools succeed, refuse, fault, fill the pod and dial out."""
    server = FastMCP("rpc-probe")

    @server.tool()
    def echo(text: str, scale: float | None = None) -> dict[str, str]:
        """Return what was passed in."""
        return {"text": text, "scale": repr(scale)}

    @server.tool()
    def whoami() -> dict[str, str]:
        """Report the caller bound for this call."""
        caller = current_caller()
        return {"actor": caller.actor, "session": caller.session, "correlation": caller.correlation}

    @server.tool()
    def boom_internal() -> dict[str, str]:
        """Raise a fault whose text must never reach the caller."""
        raise RuntimeError("PGPASSWORD=hunter2 at postgres.internal:5432")

    @server.tool()
    def boom_domain() -> dict[str, str]:
        """Raise a refusal that is the whole content of the answer."""
        raise ValueError("unknown solvent 'unobtainium'; see the vendored solvent table")

    @server.tool()
    def boom_secret() -> dict[str, str]:
        """Raise a refusal whose text carries a secret."""
        raise ValueError("could not parse config PGPASSWORD=hunter2 at postgres.internal:5432")

    @server.tool()
    def boom_full() -> dict[str, str]:
        """Refuse as a full pod does."""
        raise Admission(1, server=SERVER).refuse("the pod is full")

    @server.tool()
    def boom_budget() -> dict[str, str]:
        """Stop as the pod's clock does."""
        raise TimeBudgetExceeded("[rpc-probe-time-budget] a probe exceeded this server's budget")

    @server.tool()
    def overflow(x: float) -> dict[str, float]:
        """Return `x` scaled past the largest double: infinity for any `x` above 1."""
        return {"scaled": x * 1e308}

    @server.tool()
    def dial_out() -> dict[str, str]:
        """Open a connection to a non-loopback address, which the egress guard must refuse."""
        socket.create_connection(("93.184.216.34", 80), timeout=1.0)
        return {"reached": "yes"}

    return server


@pytest.fixture(scope="module")
def running_server(serving: Callable[..., Any]) -> Iterator[str]:
    """The probe under uvicorn on loopback, wrapped by the real `connector_app` with `rpc=`."""
    os.environ[TOKEN_ENV] = TOKEN
    app = connector_app(
        _probe_server(),
        name=SERVER,
        token_env=TOKEN_ENV,
        max_request_bytes=MAX_BYTES,
        rpc=MODELS,
    )
    with serving(app, require_ready=True) as base:
        yield base


def _post(
    base: str, operation: str, body: object, headers: dict[str, str] | None = None
) -> httpx.Response:
    """One authenticated `POST /rpc/<operation>` with `body` as JSON."""
    return httpx.post(
        f"{base}{rpc_path(operation)}",
        json=body,
        headers={"Authorization": f"Bearer {TOKEN}", **(headers or {})},
        timeout=15.0,
    )


def _sample(name: str, **labels: str) -> float:
    """A counter's current value, 0 where the series has not been created."""
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_a_call_returns_the_tools_structured_answer(running_server: str) -> None:
    """The body is the answer itself: no envelope, no content blocks."""
    response = _post(running_server, "echo", EchoRequest(text="hi").wire())
    assert response.status_code == 200
    assert response.json() == {"text": "hi", "scale": "None"}
    assert response.headers["content-type"] == "application/json"


def test_the_request_model_is_the_gate_for_the_body(running_server: str) -> None:
    """An argument the contract does not name, a missing one and a non-object are all `refused`."""
    for body in ({"text": "hi", "colour": "red"}, {}, ["text"], "text"):
        response = _post(running_server, "echo", body)
        assert response.status_code == 400, body
        assert RpcError.model_validate(response.json()).kind == "refused", body
    unparsable = httpx.post(
        f"{running_server}/rpc/echo",
        content=b"{not json",
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        timeout=15.0,
    )
    assert unparsable.status_code == 400
    assert RpcError.model_validate(unparsable.json()).kind == "refused"


def test_an_unserved_operation_is_a_404_and_the_served_set_is_the_models(
    running_server: str,
) -> None:
    """The route serves exactly `models`: a tool outside it is not reachable here."""
    response = _post(running_server, "not_a_tool", {})
    assert response.status_code == 404
    error = RpcError.model_validate(response.json())
    assert error.kind == "unknown_op"
    assert all(f"'{name}'" in error.message for name in MODELS)


def test_a_get_is_not_a_call(running_server: str) -> None:
    """Only POST calls an operation: a GET is not routed to it."""
    response = httpx.get(
        f"{running_server}/rpc/echo", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=5.0
    )
    assert response.status_code in (404, 405)


def test_every_arm_of_the_bearer_rule_applies(running_server: str) -> None:
    """No header, a wrong token, a wrong scheme and an unset variable are all 401."""
    url = f"{running_server}/rpc/echo"
    body = EchoRequest(text="hi").wire()
    assert httpx.post(url, json=body, timeout=5.0).status_code == 401
    wrong = {"Authorization": f"Bearer {TOKEN}-wrong"}
    assert httpx.post(url, json=body, headers=wrong, timeout=5.0).status_code == 401
    scheme = {"Authorization": f"Basic {TOKEN}"}
    assert httpx.post(url, json=body, headers=scheme, timeout=5.0).status_code == 401
    del os.environ[TOKEN_ENV]
    try:
        assert _post(running_server, "echo", body).status_code == 401
    finally:
        os.environ[TOKEN_ENV] = TOKEN
    assert _post(running_server, "echo", body).status_code == 200


def test_an_unauthenticated_call_does_not_reveal_whether_the_operation_exists(
    running_server: str,
) -> None:
    """Auth is middleware, so an unknown operation and a known one are both 401 without a token."""
    for operation in ("echo", "not_a_tool"):
        url = f"{running_server}/rpc/{operation}"
        assert httpx.post(url, json={}, timeout=5.0).status_code == 401


def test_the_body_cap_applies(running_server: str) -> None:
    """A declared oversize is 413 before the route reads it; so is a chunked one."""
    big = {"text": "x" * (MAX_BYTES * 2)}
    assert _post(running_server, "echo", big).status_code == 413

    def chunks() -> Iterator[bytes]:
        for _ in range(8):
            yield b"x" * MAX_BYTES

    chunked = httpx.post(
        f"{running_server}/rpc/echo",
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        content=chunks(),
        timeout=10.0,
    )
    assert chunked.status_code == 413


def test_the_identity_headers_are_read_per_request(running_server: str) -> None:
    """Each call reads its own headers; an absent header is empty, not an error."""
    stamped = {
        "X-Chemclaw-Actor": ACTOR,
        "X-Chemclaw-Session": SESSION,
        "X-Chemclaw-Correlation-Id": CORRELATION,
        "X-Chemclaw-Dry-Run": "false",
    }
    seen = _post(running_server, "whoami", {}, stamped).json()
    assert seen == {"actor": ACTOR, "session": SESSION, "correlation": CORRELATION}
    other = _post(running_server, "whoami", {}, {"X-Chemclaw-Actor": "bob"}).json()
    assert other == {"actor": "bob", "session": "", "correlation": ""}
    assert _post(running_server, "whoami", {}).json() == {
        "actor": "",
        "session": "",
        "correlation": "",
    }


def test_the_request_line_logs_the_caller_and_the_dry_run_flag(
    running_server: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Identity is logged, never decided on: the access line carries all four headers."""
    with caplog.at_level(logging.INFO, logger="mcp_server_kit.auth"):
        _post(
            running_server,
            "whoami",
            {},
            {
                "X-Chemclaw-Actor": ACTOR,
                "X-Chemclaw-Session": SESSION,
                "X-Chemclaw-Correlation-Id": CORRELATION,
                "X-Chemclaw-Dry-Run": "true",
            },
        )
    lines = [
        record.getMessage()
        for record in caplog.records
        if record.name == "mcp_server_kit.auth" and "/rpc/whoami" in record.message
    ]
    assert lines, "the server logged no access line for the RPC call"
    assert f"actor={ACTOR}" in lines[-1]
    assert f"session={SESSION}" in lines[-1]
    assert f"correlation={CORRELATION}" in lines[-1]
    assert "dry_run=true" in lines[-1]


def test_a_fault_is_replaced_once_and_joined_to_the_log_by_its_error_id(
    running_server: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-`ValueError` is a 500 whose text is generic; the real cause is in the log only."""
    with caplog.at_level(logging.ERROR):
        response = _post(running_server, "boom_internal", {})
    assert response.status_code == 500
    error = RpcError.model_validate(response.json())
    assert error.kind == "failed"
    assert "hunter2" not in response.text
    assert "postgres.internal" not in response.text
    error_id = error.message.rsplit("error id ", 1)[1].rstrip(")")
    assert any(getattr(record, "error_id", None) == error_id for record in caplog.records), (
        "no log record carries the error id the caller was given"
    )


def test_a_refusal_passes_and_a_secret_inside_it_is_redacted(running_server: str) -> None:
    """A `ValueError` is a 400 the caller can read; a credential in its text is not."""
    plain = _post(running_server, "boom_domain", {})
    assert plain.status_code == 400
    assert "unobtainium" in RpcError.model_validate(plain.json()).message
    secret = _post(running_server, "boom_secret", {})
    assert secret.status_code == 400
    assert "hunter2" not in secret.text
    assert "PGPASSWORD=***" in RpcError.model_validate(secret.json()).message


def test_a_full_pod_is_503_with_its_marker_at_the_head(running_server: str) -> None:
    """The fleet's `[<server>-at-capacity]` marker leads `message`, with no transport prefix."""
    response = _post(running_server, "boom_full", {})
    assert response.status_code == 503
    error = RpcError.model_validate(response.json())
    assert error.kind == "at_capacity"
    assert error.message.startswith(f"[{SERVER}-at-capacity] ")


def test_a_spent_clock_is_its_own_kind_with_its_marker_at_the_head(running_server: str) -> None:
    """The input was fine and a retry spends the same clock: neither `refused` nor a fault."""
    response = _post(running_server, "boom_budget", {})
    assert response.status_code == 422
    error = RpcError.model_validate(response.json())
    assert error.kind == "time_budget"
    assert error.message.startswith("[rpc-probe-time-budget] ")


def test_a_body_the_model_refuses_is_counted_as_the_tools_refusal(running_server: str) -> None:
    """The tool manager never sees it, so the route books it; unknown operations are not booked."""
    labels = {"server": SERVER, "tool": "echo", "outcome": "refused"}
    before = _sample("chemclaw_mcp_tool_calls_total", **labels)
    _post(running_server, "echo", {"colour": "red"})
    _post(running_server, "echo", {"text": "hi", "scale": 1.0, "colour": "red"})
    _post(running_server, "not_a_tool", {})
    assert _sample("chemclaw_mcp_tool_calls_total", **labels) == before + 2
    unknown = {"server": SERVER, "tool": "not_a_tool", "outcome": "refused"}
    assert _sample("chemclaw_mcp_tool_calls_total", **unknown) == 0


def test_a_non_finite_number_is_refused_whichever_way_it_is_spelled(running_server: str) -> None:
    """A bare `NaN` is refused before parsing; the string `"nan"` by the tool's argument model."""
    literal = httpx.post(
        f"{running_server}/rpc/echo",
        content=b'{"text": "x", "scale": NaN}',
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        timeout=10.0,
    )
    assert literal.status_code == 400
    assert RpcError.model_validate(literal.json()).kind == "refused"
    assert "finite" in literal.text
    spelled = _post(running_server, "echo", {"text": "x", "scale": "nan"})
    assert spelled.status_code == 400
    assert "finite" in RpcError.model_validate(spelled.json()).message


def test_a_non_finite_answer_is_a_refusal_not_a_fault(running_server: str) -> None:
    """JSON cannot carry what the tool computed, so it is a 400 refusal, not a 500 to retry."""
    response = _post(running_server, "overflow", OverflowRequest(x=10.0).wire())
    assert response.status_code == 400, response.text
    refusal = RpcError.model_validate(response.json())
    assert refusal.kind == "refused"
    assert "overflow computed answer['scaled']" in refusal.message
    assert "finite" in refusal.message
    assert _post(running_server, "overflow", OverflowRequest(x=1.0).wire()).json() == {
        "scaled": 1e308
    }


def test_the_host_rule_applies(running_server: str) -> None:
    """A `Host` the pod is not addressed by is refused as on `/mcp`; so is a non-JSON body type."""
    foreign = _post(running_server, "echo", {"text": "x"}, {"Host": "evil.example"})
    assert foreign.status_code == 421
    assert RpcError.model_validate(foreign.json()).kind == "rejected"
    typed = httpx.post(
        f"{running_server}/rpc/echo",
        content=b"text=x",
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "text/plain"},
        timeout=10.0,
    )
    assert typed.status_code == 400
    assert RpcError.model_validate(typed.json()).kind == "rejected"


def test_the_services_own_name_is_admitted_when_the_deployment_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`MCP_ALLOWED_HOSTS` opens `/rpc` to the Service name exactly as it opens `/mcp`."""
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "chemclaw-mcp-probe:8899")
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    app = connector_app(
        _probe_server(), name="rpc-probe-hosts", token_env=TOKEN_ENV, rpc={"echo": EchoRequest}
    )
    client = TestClient(app, base_url="http://127.0.0.1:8899")
    headers = {"Authorization": f"Bearer {TOKEN}"}
    named = client.post(
        "/rpc/echo", json={"text": "x"}, headers={**headers, "Host": "chemclaw-mcp-probe:8899"}
    )
    assert named.status_code == 200
    other = client.post(
        "/rpc/echo", json={"text": "x"}, headers={**headers, "Host": "chemclaw-mcp-other:8899"}
    )
    assert other.status_code == 421


def test_the_egress_guard_is_armed_on_this_path_too(running_server: str) -> None:
    """A tool that dials a non-loopback address is refused, counted, and answered as a fault."""
    before = _sample("chemclaw_mcp_egress_refused_total")
    response = _post(running_server, "dial_out", {})
    assert response.status_code == 500
    assert RpcError.model_validate(response.json()).kind == "failed"
    assert _sample("chemclaw_mcp_egress_refused_total") > before


def test_the_metrics_carry_the_operation_and_nothing_about_the_caller(running_server: str) -> None:
    """Requests are counted by the clamped path; calls by the served tool; callers by neither."""
    served = _sample("chemclaw_mcp_requests_total", server=SERVER, path="/rpc/echo", status="200")
    called = _sample("chemclaw_mcp_tool_calls_total", server=SERVER, tool="echo", outcome="ok")
    other = _sample("chemclaw_mcp_requests_total", server=SERVER, path="<other>", status="404")
    stamped = {
        "X-Chemclaw-Actor": ACTOR,
        "X-Chemclaw-Session": SESSION,
        "X-Chemclaw-Correlation-Id": CORRELATION,
    }
    _post(running_server, "echo", {"text": "CC(=O)Oc1ccccc1C(=O)O"}, stamped)
    _post(running_server, "invented_by_the_caller", {}, stamped)
    exposition = httpx.get(f"{running_server}/metrics", timeout=5.0).text
    assert (
        _sample("chemclaw_mcp_requests_total", server=SERVER, path="/rpc/echo", status="200")
        == served + 1
    )
    assert (
        _sample("chemclaw_mcp_tool_calls_total", server=SERVER, tool="echo", outcome="ok")
        == called + 1
    )
    assert (
        _sample("chemclaw_mcp_requests_total", server=SERVER, path="<other>", status="404")
        == other + 1
    )
    for private in (ACTOR, SESSION, CORRELATION, "CC(=O)Oc1ccccc1C(=O)O", "invented_by_the_caller"):
        assert private not in exposition, f"/metrics published {private!r}"


def test_an_operation_the_server_does_not_serve_is_refused_at_build() -> None:
    """A mapping to a tool that does not exist, or to another tool's model, fails at startup."""

    class GhostRequest(WireRequest):
        """A model for a tool this probe does not have."""

        tool_name: ClassVar[str] = "ghost"

    with pytest.raises(ValueError, match="not a tool this server serves"):
        connector_app(_probe_server(), name="rpc-ghost", rpc={"ghost": GhostRequest})
    with pytest.raises(ValueError, match="is for 'echo'"):
        connector_app(_probe_server(), name="rpc-swapped", rpc={"whoami": EchoRequest})
