"""The guard is only worth having if it bites, so these tests make it bite.

A guard that is installed but never proven is the same class of artifact as a README asserting a
deletion that never happened. Each test below is one property the design claims: the default is on,
loopback still works (or the server could not serve), a real outbound address is refused, the
allowlist is the only way through, and serving calls are untouched.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest
from mcp_server_kit import egress


@pytest.fixture(autouse=True)
def restore_guard() -> Iterator[None]:
    """Leave the guard exactly as armed as it was, whatever a test in here does to it."""
    yield
    egress.disarm()
    egress.arm()


def test_the_guard_is_armed_by_default() -> None:
    """Importing the kit arms it — nothing in a server has to remember to."""
    assert egress.armed()


def test_the_default_allowlist_is_empty() -> None:
    """No shipped deployment permits any host. An entry here would be a real loosening."""
    assert egress.allowed_hosts() == frozenset()


def test_a_remote_address_is_refused() -> None:
    """The whole claim, in one assertion: a socket to somewhere else does not open.

    Uses a documentation-range address (TEST-NET-3, RFC 5737) so that the *only* reason this test
    can pass is the guard: even unguarded, nothing is listening there, and the test would then fail
    with a timeout rather than an `EgressForbidden`.
    """
    with (
        pytest.raises(egress.EgressForbidden, match=r"203\.0\.113\.10"),
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client,
    ):
        client.connect(("203.0.113.10", 443))


def test_a_hostname_is_refused_without_resolving_it() -> None:
    """A name is refused as a name. Resolving it first would itself be a call out."""
    with (
        pytest.raises(egress.EgressForbidden),
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client,
    ):
        client.connect(("example.invalid", 443))


def test_connect_ex_is_guarded_too() -> None:
    """`connect_ex` returns errors instead of raising them, so it is the quieter way out."""
    with (
        pytest.raises(egress.EgressForbidden),
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client,
    ):
        client.connect_ex(("203.0.113.10", 443))


def test_loopback_still_connects() -> None:
    """A guard that blocked loopback would block the server's own tests and probes."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.connect(("127.0.0.1", port))


def test_serving_is_untouched() -> None:
    """bind/listen/accept are different calls; an armed process still answers requests."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        assert listener.getsockname()[1] > 0


def test_the_allowlist_is_the_only_way_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicitly allowed host is permitted — and nothing else becomes permitted with it."""
    monkeypatch.setenv("MCP_EGRESS_ALLOW", "203.0.113.10")
    egress.disarm()
    egress.arm()
    assert "203.0.113.10" in egress.allowed_hosts()
    with (
        pytest.raises(egress.EgressForbidden, match=r"203\.0\.113\.11"),
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client,
    ):
        client.connect(("203.0.113.11", 443))
