"""The runtime egress guard: a server in this repository never calls out, and this is what says so.

Every server here answers from data that was put on disk at build time or mounted read-only. That
is a *deployment* property in the end — a NetworkPolicy with an empty `egress:` block — but a
control that exists only in the chart is a control nobody can test, and one that exists only in
`CLAUDE.md` is a sentence. Chemclaw3 has recorded that failure twice: a README asserting a deletion
that had not happened, and `NoAuth`'s docstring describing a validator that did not exist. So the
rule is armed inside the process too.

**What it catches that a static scan cannot.** `tests/test_no_egress.py` reads our own modules and
proves *we* import no HTTP client. It says nothing about the third-party code underneath: an ML
library fetching model weights on first use, a package phoning home with usage telemetry, a licence
check over DNS. Those are the realistic ways a "no egress" claim stops being true.

**They do not all end at `socket.socket.connect`, and believing they did left two of the three
examples above uncovered.** A licence check over DNS never calls `connect` at all — `getaddrinfo`
and `gethostbyname` are module-level C functions, and an armed process resolved any name it liked,
which is a full round trip to a resolver. A datagram socket never calls `connect` either, so
`sendto`/`sendmsg` carried payload straight out. Both were measured, and both are patched here now,
along with `connect`/`connect_ex`.

**What it deliberately does not touch, and what covers each instead.** Serving is
`bind`/`listen`/`accept`, which are different calls, so an armed process still answers requests
normally. Loopback stays open: a sidecar, a health probe against ourselves, and the in-process test
client all live there. Non-`AF_INET` families (Unix sockets) pass, because they cannot leave the
host. And three channels are outside any in-process patch by construction — a **child process**
(`subprocess`, `os.system`), a **`ctypes` call straight into `libc.connect`**, and any syscall made
from a compiled extension. Those are `make offline-run`'s job: it takes the network namespace away,
which is the only layer that does not depend on the caller going through Python. `servers/pyexec`
is the server this matters most for, and its README states the same division — the child process
and its rlimits are the boundary there, not the guards inside the parent.

The allowlist is `MCP_EGRESS_ALLOW`, and it is **empty by default and empty in the shipped chart**.
It exists so a build-time ingestion step — the one sanctioned moment a dataset is fetched — can run
with the guard relaxed *outside* the serving image, not so a server can be talked into calling out.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Iterable, Sequence
from typing import Any

__all__ = ["EgressForbidden", "allowed_hosts", "arm", "armed", "disarm"]


class EgressForbidden(OSError):
    """A server tried to open an outbound connection. That is a bug, not a configuration problem.

    Derived from `OSError` so it surfaces where a connection error would, but named so nobody has
    to guess: a stack trace ending here names the library that tried to call out.
    """


_ALLOW_ENV = "MCP_EGRESS_ALLOW"
_GUARD_ENV = "MCP_EGRESS_GUARD"

_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
_original_sendto = socket.socket.sendto
_original_sendmsg = socket.socket.sendmsg
_original_getaddrinfo = socket.getaddrinfo
_original_gethostbyname = socket.gethostbyname
_original_gethostbyname_ex = socket.gethostbyname_ex

_armed = False
_allowed: frozenset[str] = frozenset()


def allowed_hosts() -> frozenset[str]:
    """The hosts the guard currently permits, beyond loopback. Empty in every shipped deployment."""
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

    Literal addresses are decided by `ipaddress`, so the whole of `127.0.0.0/8` and `::1` count
    rather than the two spellings people remember. A name is only loopback if it *is* `localhost`
    (or a subdomain of it): resolving an arbitrary name here would mean a DNS lookup, which is
    itself a call out, and a guard whose check leaks is not a guard.

    **The unspecified address counts too, and it started mattering when this guard grew to cover
    `getaddrinfo`.** `0.0.0.0` and `::` are what a container binds to, not somewhere to reach — and
    a `connect` to either lands on this machine anyway. CPython short-circuits a numeric bind
    address before it reaches `getaddrinfo`, so nothing observed this; a guard that refuses a
    server's own bind on some other path would be an outage rather than a control.
    """
    bare = host.strip("[]").lower()
    if bare in {"localhost", ""}:
        return True
    if bare.endswith(".localhost"):
        return True
    try:
        parsed = ipaddress.ip_address(bare.split("%", 1)[0])
    except ValueError:
        return False
    return parsed.is_loopback or parsed.is_unspecified


def _host_of(address: Any) -> str | None:
    """The destination host of a `connect()` argument, or `None` when the family cannot leave.

    `AF_INET`/`AF_INET6` pass a tuple whose first element is the host. Everything else — a Unix
    socket path (a `str`/`bytes`), an `AF_NETLINK` tuple of ints — cannot reach another machine,
    so it is not this guard's business.

    **The host inside the tuple may be `bytes`, and reading only `str` was a complete bypass.**
    CPython accepts `connect((b"1.1.1.1", 80))`, so a tuple built from an already-encoded buffer
    produced `None` here and `_check` read that as a family that cannot leave the host. The two
    shapes stay distinguishable — a Unix address *is* the `bytes`, an inet address *contains* one
    — so the branch above still exempts the path case and only the element is decoded.
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


def _check(address: Any) -> None:
    """Raise `EgressForbidden` unless `address` is loopback or explicitly allowed."""
    host = _host_of(address)
    if host is None or _is_loopback(host) or host.strip("[]").lower() in _allowed:
        return
    raise EgressForbidden(
        f"outbound connection to {host!r} refused: servers in this repository answer from "
        f"vendored data and never call out at request time. If a build-time ingestion step needs "
        f"this host, run it outside the serving image with {_ALLOW_ENV} set."
    )


def arm(allow: Iterable[str] = ()) -> None:
    """Install the guard. Idempotent, so importing two servers in one process is safe.

    Args:
        allow: Extra hosts to permit, merged with `MCP_EGRESS_ALLOW`. Callers should leave this
            empty; it is here for the ingestion scripts that run outside the serving image.
    """
    global _armed, _allowed
    _allowed = _parse_allow(os.environ.get(_ALLOW_ENV)) | frozenset(h.lower() for h in allow)
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
        # A name resolution is a round trip to a resolver, so it is egress in its own right — and
        # it is the shape a licence check takes. `host` is `None` for an `AI_PASSIVE` bind lookup,
        # which `_host_of` reads as "nothing to leave for".
        _check((host, port))
        return _original_getaddrinfo(host, port, *args, **kwargs)

    def gethostbyname(hostname: Any) -> Any:
        _check((hostname, 0))
        return _original_gethostbyname(hostname)

    def gethostbyname_ex(hostname: Any) -> Any:
        _check((hostname, 0))
        return _original_gethostbyname_ex(hostname)

    socket.socket.connect = connect  # type: ignore[method-assign,assignment]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign,assignment]
    socket.socket.sendto = sendto  # type: ignore[method-assign,assignment]
    socket.socket.sendmsg = sendmsg  # type: ignore[method-assign,assignment]
    socket.getaddrinfo = getaddrinfo
    socket.gethostbyname = gethostbyname
    socket.gethostbyname_ex = gethostbyname_ex
    _armed = True


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
    _armed = False
    _allowed = frozenset()


def arm_from_env() -> None:
    """Arm unless `MCP_EGRESS_GUARD` is explicitly `off`.

    Called from this package's `__init__`, so importing any part of a server arms it. The opt-out
    exists for the ingestion scripts and for debugging; it is not set in any shipped deployment,
    and `tests/test_egress.py` asserts the default is on.
    """
    if os.environ.get(_GUARD_ENV, "on").strip().lower() in {"off", "0", "false", "no"}:
        return
    arm()
