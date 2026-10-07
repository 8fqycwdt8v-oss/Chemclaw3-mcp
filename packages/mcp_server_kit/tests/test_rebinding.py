"""The DNS-rebinding guard on `/mcp`, against a real socket: on, and told the Service name.

Dialling `127.0.0.1` sends a loopback `Host`, which upstream's default admits; a cluster caller
sends a Service name and would get 421. So each test sets that `Host` over a real uvicorn
listener, since the guard runs inside the mounted MCP app.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp_server_kit import rebinding
from mcp_server_kit.app import connector_app

SERVICE_HOST = "chemclaw-mcp-probe:8859"

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "rebinding-probe", "version": "0"},
    },
}


def _probe() -> FastMCP:
    server = FastMCP("rebinding-probe")

    @server.tool()
    def echo(text: str) -> str:
        """Return what was passed in."""
        return text

    return server


def _initialize(base: str, host: str | None) -> httpx.Response:
    """A raw MCP `initialize` POST, as a cluster caller sends it, with `host` as its `Host`."""
    headers = {"accept": "application/json, text/event-stream"}
    if host is not None:
        headers["host"] = host
    return httpx.post(f"{base}/mcp", json=INITIALIZE, headers=headers, timeout=15.0)


def test_a_service_name_is_refused_without_the_variable(
    serving: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the variable, a Service-name `Host` gets 421 while loopback still gets 200.

    Both halves, because a 421 for everything would also pass the first assertion.
    """
    monkeypatch.delenv(rebinding.ALLOWED_HOSTS_ENV, raising=False)
    with serving(connector_app(_probe(), name="rebinding-unset")) as base:
        refused = _initialize(base, SERVICE_HOST)
        assert refused.status_code == 421, refused.text
        served = _initialize(base, None)
        assert served.status_code == 200, served.text
        # `/healthz` is outside the MCP app, which is why every probe stayed green.
        assert httpx.get(f"{base}/healthz", headers={"host": SERVICE_HOST}).status_code == 200


def test_a_listed_service_name_completes_the_handshake(
    serving: Callable[..., Any],
    mcp_session: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the variable set, the cluster caller's `Host` initialises a session and calls a tool."""
    monkeypatch.setenv(rebinding.ALLOWED_HOSTS_ENV, f"{SERVICE_HOST}, other-name:*")
    with serving(connector_app(_probe(), name="rebinding-listed")) as base:
        raw = _initialize(base, SERVICE_HOST)
        assert raw.status_code == 200, raw.text
        assert "rebinding-probe" in raw.text

        async def _call() -> str:
            async with mcp_session(base, headers={"host": SERVICE_HOST}) as session:
                result = await session.call_tool("echo", {"text": "through the Service"})
                return str(result.content[0].text)

        assert asyncio.run(_call()) == "through the Service"
        # The `host:*` form, at a port nobody wrote down.
        assert _initialize(base, "other-name:31337").status_code == 200
        # Loopback is still admitted: the variable adds, it does not replace.
        assert _initialize(base, None).status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        "evil.example:8859",  # an unlisted name
        "chemclaw-mcp-probe:8860",  # the listed name on a port that was not listed
        "chemclaw-mcp-probe",  # the listed name with no port at all
        "chemclaw-mcp-probe:8859.evil.example",  # a suffix trick on the exact match
    ],
)
def test_an_unlisted_host_is_still_refused(
    serving: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """The guard is still a guard: only what was listed is admitted."""
    monkeypatch.setenv(rebinding.ALLOWED_HOSTS_ENV, SERVICE_HOST)
    with serving(connector_app(_probe(), name="rebinding-unlisted")) as base:
        assert _initialize(base, host).status_code == 421


def test_a_foreign_origin_is_refused_even_on_a_listed_host(
    serving: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `Origin` half of the guard: a browser page elsewhere cannot ride a listed `Host`."""
    monkeypatch.setenv(rebinding.ALLOWED_HOSTS_ENV, SERVICE_HOST)
    with serving(connector_app(_probe(), name="rebinding-origin")) as base:
        headers = {
            "accept": "application/json, text/event-stream",
            "host": SERVICE_HOST,
            "origin": "http://evil.example",
        }
        response = httpx.post(f"{base}/mcp", json=INITIALIZE, headers=headers, timeout=15.0)
        assert response.status_code == 403
        headers["origin"] = f"http://{SERVICE_HOST}"
        response = httpx.post(f"{base}/mcp", json=INITIALIZE, headers=headers, timeout=15.0)
        assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ("chemclaw-mcp-probe:8859,", "empty"),
        ("a:1,,b:2", "empty"),
        (" , ", "empty"),
        ("chemclaw mcp:8859", "whitespace"),
        ("http://chemclaw-mcp-probe:8859", "is a URL"),
        ("chemclaw-mcp-probe:8859/mcp", "is a URL"),
        ("user@chemclaw-mcp-probe:8859", "is a URL"),
        ("*", "wildcard host"),
        ("*:*", "wildcard host"),
        ("*:8859", "wildcard host"),
        ("*.svc:8859", "wildcard host"),
        ("chemclaw-mcp-probe", "has no port"),
        ("chemclaw-mcp-probe:", "port"),
        ("chemclaw-mcp-probe:0", "port"),
        ("chemclaw-mcp-probe:65536", "port"),
        ("chemclaw-mcp-probe:http", "port"),
        ("Chemclaw-MCP-probe:8859", "lower-case"),
        ("-bad-:8859", "lower-case"),
        ("::1:8859", "unbracketed IPv6"),
        ("[::1", "no `]:port`"),
        ("[::1]", "no `]:port`"),
        ("[not-v6]:8859", "IPv6"),
    ],
)
def test_an_entry_the_guard_cannot_use_is_refused_at_startup(
    monkeypatch: pytest.MonkeyPatch, raw: str, fragment: str
) -> None:
    """Refused when the app is built — at import, for every server — naming the variable."""
    monkeypatch.setenv(rebinding.ALLOWED_HOSTS_ENV, raw)
    with pytest.raises(ValueError, match=rebinding.ALLOWED_HOSTS_ENV) as refused:
        connector_app(_probe(), name="rebinding-invalid")
    assert fragment in str(refused.value)


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_unset_is_exactly_upstream_s_loopback_default(raw: str | None) -> None:
    """Unset, the settings are the ones upstream builds for `FastMCP("x")` — transcribed, and held.

    Read off a freshly constructed `FastMCP` rather than restated, so an upstream change to its own
    defaults is a red test here rather than a silent change to what this fleet admits.
    """
    upstream = FastMCP("upstream-default").settings.transport_security
    assert upstream is not None
    ours = rebinding.transport_security(raw)
    assert ours == upstream
    assert ours.enable_dns_rebinding_protection is True


def test_entries_are_added_after_loopback_in_order_without_duplicates() -> None:
    raw = "b-host:2, [::1]:9000, b-host:2, 10.0.0.7:*"
    settings = rebinding.transport_security(raw)
    assert settings.allowed_hosts == [
        *rebinding.LOOPBACK_HOSTS,
        "b-host:2",
        "[::1]:9000",
        "10.0.0.7:*",
    ]
    assert settings.allowed_origins == [
        *rebinding.LOOPBACK_ORIGINS,
        "http://b-host:2",
        "http://[::1]:9000",
        "http://10.0.0.7:*",
    ]


def test_a_server_built_with_the_guard_off_is_guarded_anyway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`FastMCP(host="0.0.0.0")` arrives with no guard at all; `connector_app` installs one."""
    monkeypatch.delenv(rebinding.ALLOWED_HOSTS_ENV, raising=False)
    server = FastMCP("rebinding-wide", host="0.0.0.0")
    assert server.settings.transport_security is None
    connector_app(server, name="rebinding-wide")
    settings = server.settings.transport_security
    assert settings is not None
    assert settings.enable_dns_rebinding_protection is True
    assert settings.allowed_hosts == list(rebinding.LOOPBACK_HOSTS)
