"""The runtime egress guard: a server in this repository never calls out.

Armed in-process because it catches what a static scan of our own modules cannot — third-party
code fetching weights, sending telemetry, or checking a licence over DNS. `arm()` rebinds the
outbound calls (connects, datagram sends, forward and reverse name lookups); a refusal is
logged, counted on `chemclaw_mcp_egress_refused_total`, and raised as `EgressForbidden`.

Serving (`bind`/`listen`/`accept`), loopback and non-inet families pass. Outside any in-process
patch by construction: a child process, `ctypes` into libc, the C type `_socket.socket`, and
syscalls from compiled extensions — `no_egress.py` refuses the importable ones statically, and
`make offline-run` removes the network for the rest.

`MCP_EGRESS_ALLOW` is empty by default and in every shipped deployment; it exists for build-time
ingestion outside the serving image.
"""

from __future__ import annotations

import ipaddress
import logging
import os

# The guard is the rebinding of this module's methods, so this file must import `socket`.
import socket  # noqa: TID253
import threading
from collections.abc import Iterable, Sequence
from typing import Any

from mcp_server_kit.metrics import EGRESS_ALLOWED_HOSTS, EGRESS_GUARD_ARMED, EGRESS_REFUSED

__all__ = [
    "GUARD_DISABLED_VALUES",
    "EgressForbidden",
    "allowed_hosts",
    "arm",
    "armed",
    "disarm",
]

logger = logging.getLogger(__name__)


class EgressForbidden(OSError):
    """A server tried to open an outbound connection. That is a bug, not a configuration problem.

    An `OSError` so it surfaces where a connection error would; the stack trace names the caller.
    """


_ALLOW_ENV = "MCP_EGRESS_ALLOW"
_GUARD_ENV = "MCP_EGRESS_GUARD"

# The `MCP_EGRESS_GUARD` values that mean "do not arm"; everything else arms, the empty string
# included. Public because `tests/test_fleet_*.py` refuses any shipped value it cannot prove arms.
GUARD_DISABLED_VALUES = frozenset({"off", "0", "false", "no"})

_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
_original_sendto = socket.socket.sendto
_original_sendmsg = socket.socket.sendmsg
_original_getaddrinfo = socket.getaddrinfo
_original_gethostbyname = socket.gethostbyname
_original_gethostbyname_ex = socket.gethostbyname_ex
_original_getnameinfo = socket.getnameinfo
_original_gethostbyaddr = socket.gethostbyaddr

_armed = False
_allowed: frozenset[str] = frozenset()


def allowed_hosts() -> frozenset[str]:
    """The hosts the guard currently permits, beyond loopback. Empty in every shipped deployment.

    Its size (never its contents) is published as `chemclaw_mcp_egress_allowed_hosts`.
    """
    return _allowed


def armed() -> bool:
    """Whether the guard is currently installed."""
    return _armed


def _parse_allow(raw: str | None) -> frozenset[str]:
    """Split `MCP_EGRESS_ALLOW` into a host set, ignoring blanks and surrounding whitespace."""
    if not raw:
        return frozenset()
    return frozenset(host.strip().lower() for host in raw.split(",") if host.strip())


def _is_loopback(host: str) -> bool:
    """Whether `host` names this machine — the one destination that is never egress.

    Loopback and unspecified IP literals (decided by `ipaddress`) and the exact name `localhost`
    pass. No other name does — not even a `.localhost` suffix — because only the resolver knows
    where a name points, and resolving it here would itself be egress.
    """
    bare = host.strip("[]").lower()
    if bare in {"localhost", ""}:
        return True
    try:
        parsed = ipaddress.ip_address(bare.split("%", 1)[0])
    except ValueError:
        return False
    return parsed.is_loopback or parsed.is_unspecified


def _host_of(address: Any) -> str | None:
    """The destination host of a `connect()` argument, or `None` when the family cannot leave.

    `AF_INET`/`AF_INET6` pass a tuple whose first element is the host, as `str` or `bytes` (both
    read); a Unix path or `AF_NETLINK` tuple cannot reach another machine.
    """
    if isinstance(address, (str, bytes, bytearray)):
        return None
    if isinstance(address, Sequence) and address:
        head = address[0]
        if isinstance(head, (bytes, bytearray)):
            return bytes(head).decode("ascii", "replace")
        if isinstance(head, str):
            return head
    return None


# Set while a refusal is being recorded, so the recording cannot record itself; thread-local so
# concurrent refusals on two threads are both recorded.
_reporting = threading.local()


def _report(host: str) -> None:
    """Count and log one refusal, unless this is the *recording* of a refusal calling back in.

    A network-backed log handler connects on emit, which re-enters `_check`; without this guard one
    refusal would count and log many times. The inner connection is still refused.
    """
    if getattr(_reporting, "active", False):
        return
    _reporting.active = True
    try:
        EGRESS_REFUSED.inc()
        logger.error("egress refused: host=%r", host)
    finally:
        _reporting.active = False


def _check(address: Any) -> None:
    """Raise `EgressForbidden` unless `address` is loopback or explicitly allowed.

    Logged and counted before raising, because callers that catch `OSError` (retries, degraded
    ensembles, backend-resolution errors) would otherwise hide that the guard fired. Only the host
    is logged; the counter is unlabelled because the host is unbounded.
    """
    host = _host_of(address)
    if host is None or _is_loopback(host) or host.strip("[]").lower() in _allowed:
        return
    _report(host)
    raise EgressForbidden(
        f"outbound connection to {host!r} refused: servers in this repository answer from "
        f"vendored data and never call out at request time. If a build-time ingestion step needs "
        f"this host, run it outside the serving image with {_ALLOW_ENV} set."
    )


def arm(allow: Iterable[str] = ()) -> None:
    """Install the guard. Idempotent, so importing two servers in one process is safe.

    Args:
        allow: Extra hosts to permit, merged with `MCP_EGRESS_ALLOW`; for ingestion scripts outside
            the serving image only.
    """
    global _armed, _allowed
    _allowed = _parse_allow(os.environ.get(_ALLOW_ENV)) | frozenset(h.lower() for h in allow)
    # Published on every call, so a second `arm()` with a wider environment is reflected.
    EGRESS_ALLOWED_HOSTS.set(len(_allowed))
    if _armed:
        return

    def connect(self: socket.socket, address: Any) -> None:
        _check(address)
        return _original_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        _check(address)
        return _original_connect_ex(self, address)

    def sendto(self: socket.socket, *args: Any) -> int:
        # `sendto(data, address)` and `sendto(data, flags, address)` — the destination is last in
        # both, and this is the only unguarded channel that ever moved payload.
        _check(args[-1] if args else None)
        return int(_original_sendto(self, *args))

    def sendmsg(self: socket.socket, *args: Any, **kwargs: Any) -> int:
        # `sendmsg(buffers[, ancdata[, flags[, address]]])`. Forwarded verbatim so CPython's own
        # defaults apply rather than a re-declared set of them.
        address = kwargs.get("address", args[3] if len(args) >= 4 else None)
        _check(address)
        return int(_original_sendmsg(self, *args, **kwargs))

    def getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        # A name resolution is a round trip to a resolver, so it is egress. `host` is `None` for an
        # `AI_PASSIVE` bind lookup.
        _check((host, port))
        return _original_getaddrinfo(host, port, *args, **kwargs)

    def gethostbyname(hostname: Any) -> Any:
        _check((hostname, 0))
        return _original_gethostbyname(hostname)

    def gethostbyname_ex(hostname: Any) -> Any:
        _check((hostname, 0))
        return _original_gethostbyname_ex(hostname)

    def getnameinfo(sockaddr: Any, flags: Any) -> Any:
        # A reverse lookup: the address it resolves is the channel. Loopback still resolves.
        _check(sockaddr)
        return _original_getnameinfo(sockaddr, flags)

    def gethostbyaddr(ip_address: Any) -> Any:
        _check((ip_address, 0))
        return _original_gethostbyaddr(ip_address)

    socket.socket.connect = connect  # type: ignore[method-assign,assignment]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign,assignment]
    socket.socket.sendto = sendto  # type: ignore[method-assign,assignment]
    socket.socket.sendmsg = sendmsg  # type: ignore[method-assign,assignment]
    socket.getaddrinfo = getaddrinfo
    socket.gethostbyname = gethostbyname
    socket.gethostbyname_ex = gethostbyname_ex
    socket.getnameinfo = getnameinfo
    socket.gethostbyaddr = gethostbyaddr
    _armed = True
    EGRESS_GUARD_ARMED.set(1)


def disarm() -> None:
    """Restore the unguarded socket methods. For tests of the guard itself, and nothing else."""
    global _armed, _allowed
    socket.socket.connect = _original_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = _original_connect_ex  # type: ignore[method-assign]
    socket.socket.sendto = _original_sendto  # type: ignore[method-assign]
    socket.socket.sendmsg = _original_sendmsg  # type: ignore[method-assign]
    socket.getaddrinfo = _original_getaddrinfo
    socket.gethostbyname = _original_gethostbyname
    socket.gethostbyname_ex = _original_gethostbyname_ex
    socket.getnameinfo = _original_getnameinfo
    socket.gethostbyaddr = _original_gethostbyaddr
    _armed = False
    _allowed = frozenset()
    EGRESS_GUARD_ARMED.set(0)
    EGRESS_ALLOWED_HOSTS.set(0)


def arm_from_env() -> None:
    """Arm unless `MCP_EGRESS_GUARD` holds one of `GUARD_DISABLED_VALUES`.

    Called from this package's `__init__`. The opt-out is for ingestion scripts and debugging and is
    set in no shipped deployment.
    """
    if os.environ.get(_GUARD_ENV, "on").strip().lower() in GUARD_DISABLED_VALUES:
        # Published so a disarmed guard (and the allowlist it was given) is visible from a scrape.
        EGRESS_GUARD_ARMED.set(0)
        EGRESS_ALLOWED_HOSTS.set(len(_parse_allow(os.environ.get(_ALLOW_ENV))))
        return
    arm()
