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


def test_a_bytes_host_is_refused_like_a_str_one() -> None:
    """`connect((b"1.1.1.1", 80))` is a connection, and CPython accepts it.

    `_host_of` used to return the host only for a `str` first element, and `_check` reads `None`
    as "a family that cannot leave the host". So a tuple built from an already-encoded buffer was
    a complete bypass of the guard in pure Python — measured: `sock.connect((b"1.1.1.1", 80))`
    connected with the guard armed and `allowed_hosts()` empty.
    """
    with (
        pytest.raises(egress.EgressForbidden, match=r"203\.0\.113\.10"),
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client,
    ):
        client.connect((b"203.0.113.10", 443))


def test_a_bytearray_host_is_refused_too() -> None:
    """The same hole, one type over. `bytearray` is what a buffer being built looks like."""
    with (
        pytest.raises(egress.EgressForbidden),
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client,
    ):
        client.connect((bytearray(b"203.0.113.10"), 443))


def test_a_unix_socket_path_is_still_not_this_guard_s_business() -> None:
    """The bytes fix must not start refusing `AF_UNIX`, whose address *is* a path.

    The two shapes are distinguishable and always were: a Unix address is the `bytes`/`str`
    itself, an inet address is a tuple containing one. This pins the distinction, because the
    obvious way to fix the tuple case is to widen the branch above it.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        with pytest.raises(OSError) as raised:
            client.connect(b"/tmp/no-such-socket-mcp-kit")
        assert not isinstance(raised.value, egress.EgressForbidden)


def test_a_dns_lookup_is_refused() -> None:
    """The guard's own docstring names "a licence check over DNS" as a thing it catches.

    It did not: `getaddrinfo` is a module-level C call, not a `socket.socket` method, so an armed
    process resolved any name it liked — a full round trip to a resolver, which is both the
    classic covert channel and the usual shape of a licence check. Measured before the fix:
    `getaddrinfo("one.one.one.one", 80)` returned a real answer with the guard armed.
    """
    with pytest.raises(egress.EgressForbidden, match=r"example\.invalid"):
        socket.getaddrinfo("example.invalid", 443)
    with pytest.raises(egress.EgressForbidden):
        socket.gethostbyname("example.invalid")
    with pytest.raises(egress.EgressForbidden):
        socket.gethostbyname_ex("example.invalid")


def test_a_localhost_suffix_name_is_refused() -> None:
    """`x.localhost` is not loopback — the OS resolver decides what it points at.

    The guard used to exempt any name ending in `.localhost`, but such a name resolves to whatever
    the resolver (or `/etc/hosts`) says: measured, `connect(("exfil.localhost", 80))` reached a
    public address, and `getaddrinfo("<payload>.localhost")` was permitted — DNS exfiltration
    through a waved-through name. Only the exact string `localhost` and loopback IP literals pass.
    """
    with (
        pytest.raises(egress.EgressForbidden, match=r"exfil\.localhost"),
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client,
    ):
        client.connect(("exfil.localhost", 80))
    with pytest.raises(egress.EgressForbidden, match=r"payload\.localhost"):
        socket.getaddrinfo("payload.localhost", 80)


def test_exact_localhost_and_loopback_ips_still_pass() -> None:
    """Dropping the suffix rule must not touch the exact name or the loopback literals."""
    assert socket.getaddrinfo("localhost", 0, socket.AF_INET)
    assert socket.getaddrinfo("127.0.0.1", 0, socket.AF_INET)


def test_a_reverse_lookup_is_refused() -> None:
    """`getnameinfo`/`gethostbyaddr` resolve an address to a name — the same round trip, backwards.

    Both were unpatched and completed with the guard armed; the address being resolved is the
    covert channel, so both are refused now. Loopback addresses still resolve.
    """
    with pytest.raises(egress.EgressForbidden, match=r"203\.0\.113\.10"):
        socket.getnameinfo(("203.0.113.10", 80), 0)
    with pytest.raises(egress.EgressForbidden, match=r"203\.0\.113\.10"):
        socket.gethostbyaddr("203.0.113.10")


def test_reverse_lookup_of_loopback_still_works() -> None:
    """A name for the server's own socket must still resolve."""
    assert socket.getnameinfo(("127.0.0.1", 0), 0)
    assert socket.gethostbyaddr("127.0.0.1")


def test_resolving_loopback_still_works() -> None:
    """A guard that refused `localhost` would break every probe and in-process client here."""
    assert socket.getaddrinfo("127.0.0.1", 0, socket.AF_INET)
    assert socket.getaddrinfo("localhost", 0, socket.AF_INET)
    assert socket.gethostbyname("localhost").startswith("127.")


def test_a_passive_lookup_is_not_a_lookup_out() -> None:
    """`getaddrinfo(None, port, AI_PASSIVE)` is how a server asks for an address to bind."""
    assert socket.getaddrinfo(None, 0, socket.AF_INET, flags=socket.AI_PASSIVE)


def test_udp_payload_cannot_leave() -> None:
    """A connectionless socket never calls `connect`, so `sendto` was the whole channel.

    Measured before the fix: 16 bytes actually reached 8.8.8.8:53 with the guard armed. This is
    the only unguarded channel that moved payload, which is why it is closed rather than
    documented.
    """
    with (
        pytest.raises(egress.EgressForbidden, match=r"203\.0\.113\.10"),
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client,
    ):
        client.sendto(b"payload", ("203.0.113.10", 53))


def test_udp_sendmsg_cannot_leave_either() -> None:
    """`sendmsg` takes its destination as a fourth argument; the same check reads it."""
    with (
        pytest.raises(egress.EgressForbidden, match=r"203\.0\.113\.10"),
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client,
    ):
        client.sendmsg([b"payload"], [], 0, ("203.0.113.10", 53))


def test_udp_to_loopback_still_works() -> None:
    """The serving and sidecar case: a datagram to ourselves is not egress."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            assert client.sendto(b"ping", ("127.0.0.1", port)) == 4
            assert client.sendmsg([b"pong"], [], 0, ("127.0.0.1", port)) == 4
        assert listener.recv(16) == b"ping"
        assert listener.recv(16) == b"pong"


def test_a_connected_udp_socket_is_covered_by_connect() -> None:
    """`connect` on a datagram socket sets the peer, so the existing check already sees it."""
    with (
        pytest.raises(egress.EgressForbidden),
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client,
    ):
        client.connect(("203.0.113.10", 53))


def test_disarm_restores_every_patched_call() -> None:
    """`disarm` has to undo exactly what `arm` did, or a test leaves the process half-guarded."""
    egress.disarm()
    try:
        assert socket.socket.connect is not None
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.sendto(b"x", ("127.0.0.1", 9))
        socket.getaddrinfo("127.0.0.1", 0)
        socket.getnameinfo(("127.0.0.1", 0), 0)
        socket.gethostbyaddr("127.0.0.1")
        assert not egress.armed()
    finally:
        egress.arm()


def test_binding_and_resolving_the_unspecified_address_is_not_egress() -> None:
    """`0.0.0.0` and `::` are what a container binds to, not a destination.

    Nothing observed this before the DNS patch, because CPython resolves a numeric bind address
    without calling `getaddrinfo` — but a guard that could refuse a server's own bind would be an
    outage wearing a control's clothes.
    """
    assert socket.getaddrinfo("0.0.0.0", 0, socket.AF_INET)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("0.0.0.0", 0))
        listener.listen(1)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.connect(("0.0.0.0", listener.getsockname()[1]))
