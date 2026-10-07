"""The guard is only worth having if it bites, so these tests make it bite.

One property per test: on by default, loopback still works, a real outbound address is refused,
the allowlist is the only way through, and serving calls are untouched.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest
from mcp_server_kit import egress
from prometheus_client import REGISTRY, generate_latest


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

    A TEST-NET-3 address (RFC 5737) has nothing listening, so without the guard this would time out
    rather than raise `EgressForbidden`.
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

    A `bytes` host in an inet tuple must be checked like a `str` one, or it bypasses the guard.
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
    """An `AF_UNIX` address (bytes or str path, not a tuple) is still not this guard's business.

    Pins the distinction, since the obvious way to handle the bytes tuple would widen the branch.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        with pytest.raises(OSError) as raised:
            client.connect(b"/tmp/no-such-socket-mcp-kit")
        assert not isinstance(raised.value, egress.EgressForbidden)


def test_a_dns_lookup_is_refused() -> None:
    """A DNS lookup is refused.

    `getaddrinfo` is a module-level C call, not a socket method, so it must be patched separately; a
    resolver round trip is a covert channel and the usual shape of a licence check.
    """
    with pytest.raises(egress.EgressForbidden, match=r"example\.invalid"):
        socket.getaddrinfo("example.invalid", 443)
    with pytest.raises(egress.EgressForbidden):
        socket.gethostbyname("example.invalid")
    with pytest.raises(egress.EgressForbidden):
        socket.gethostbyname_ex("example.invalid")


def test_a_localhost_suffix_name_is_refused() -> None:
    """`x.localhost` is not loopback — the OS resolver decides what it points at.

    Only the exact string `localhost` and loopback IP literals pass; a `.localhost` suffix name
    could resolve anywhere and carry DNS exfiltration.
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
    the only unguarded channel that moved payload, which is why it is closed rather than documented.
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

    A guard that refused a server's own bind would be an outage.
    """
    assert socket.getaddrinfo("0.0.0.0", 0, socket.AF_INET)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("0.0.0.0", 0))
        listener.listen(1)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.connect(("0.0.0.0", listener.getsockname()[1]))


def test_recording_a_refusal_cannot_re_enter_the_guard() -> None:
    """The refusal's own log line is egress when a handler writes to the network.

    A network log handler connects on emit, so recording a refusal must not recurse into further
    refusals and inflate the counter. The inner connection is still refused; only its record is
    suppressed.
    """
    import logging.handlers

    from mcp_server_kit.metrics import EGRESS_REFUSED

    kept: list[str] = []

    class Keeper(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            kept.append(record.getMessage())

    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    # Connects lazily, on the first record it is asked to emit — which is the refusal's own line.
    network_handler = logging.handlers.SocketHandler("10.0.0.1", 5140)
    root.handlers = [Keeper(), network_handler]
    root.setLevel(logging.ERROR)
    before = EGRESS_REFUSED._value.get()
    try:
        with pytest.raises(egress.EgressForbidden):
            socket.socket().connect(("example.com", 443))
    finally:
        network_handler.close()
        root.handlers, root.level = handlers, level

    booked = EGRESS_REFUSED._value.get() - before
    assert booked == 1, f"one refused connect booked {booked} on the counter an alert reads"
    assert kept == ["egress refused: host='example.com'"], (
        f"the refusal's log lines were {kept!r}; the line naming the real destination is the one "
        "an operator greps for and it must not be lost under the log server's own refusals"
    )


def _sample(name: str) -> float:
    """One unlabelled sample's value, read off the exposition a Prometheus scrape would get."""
    prefix = f"{name} "
    for line in generate_latest(REGISTRY).decode("utf-8").splitlines():
        if line.startswith(prefix):
            return float(line[len(prefix) :])
    raise AssertionError(f"{name} is not published; a scrape cannot see it")


def test_a_widened_allowlist_is_visible_from_a_scrape(monkeypatch: pytest.MonkeyPatch) -> None:
    """A widened allowlist is visible from a scrape, as a count.

    Without it an armed guard with `MCP_EGRESS_ALLOW` set looks the same as the shipped posture. The
    count and never the host, since a destination is attacker-influenced; `> 0` is the alert.
    """
    monkeypatch.setenv("MCP_EGRESS_ALLOW", "evil.example.com, cache.example.com")
    egress.disarm()
    egress.arm()
    assert _sample("chemclaw_mcp_egress_allowed_hosts") == 2.0
    assert "evil.example.com" not in generate_latest(REGISTRY).decode("utf-8"), (
        "/metrics is unauthenticated and a destination host cannot be clamped; only the count "
        "may be published"
    )

    monkeypatch.delenv("MCP_EGRESS_ALLOW")
    egress.disarm()
    egress.arm()
    assert _sample("chemclaw_mcp_egress_allowed_hosts") == 0.0


def test_a_disabled_guard_still_publishes_the_widening_it_was_configured_with(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With `MCP_EGRESS_GUARD=off`, `arm_from_env` still publishes the configured allowlist.

    Together with `chemclaw_mcp_egress_guard_armed 0` it tells an operator whether the pod's posture
    is the shipped one.
    """
    monkeypatch.setenv("MCP_EGRESS_GUARD", "off")
    monkeypatch.setenv("MCP_EGRESS_ALLOW", "weights.example.org")
    egress.disarm()
    egress.arm_from_env()
    assert not egress.armed()
    assert _sample("chemclaw_mcp_egress_guard_armed") == 0.0
    assert _sample("chemclaw_mcp_egress_allowed_hosts") == 1.0


def test_every_value_that_disarms_the_guard_is_in_the_set_the_ratchet_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`GUARD_DISABLED_VALUES` is what `arm_from_env` does, driven rather than transcribed.

    The fleet ratchet imports this set to refuse a shipped value that disarms the guard, so it is a
    contract: every member disarms, and the likely-misread values arm. The empty string arms, since
    `""` is in no disable set. `${GUARD}` arms here but is an offence to the ratchet, because a
    build expands it to whatever it was given.
    """
    disabling = ["off", "0", "false", "no", "OFF", " off ", "False"]
    for value in disabling:
        monkeypatch.setenv("MCP_EGRESS_GUARD", value)
        egress.disarm()
        egress.arm_from_env()
        assert not egress.armed(), f"{value!r} should disarm the guard"
    for value in ["", "on", "1", "true", "${GUARD}", "anything else"]:
        monkeypatch.setenv("MCP_EGRESS_GUARD", value)
        egress.disarm()
        egress.arm_from_env()
        assert egress.armed(), f"{value!r} should arm the guard"

    # The table is the test's own data, compared against the constant in both directions; iterating
    # the constant would silently stop testing a value removed from it.
    assert {value.strip().lower() for value in disabling} == egress.GUARD_DISABLED_VALUES
