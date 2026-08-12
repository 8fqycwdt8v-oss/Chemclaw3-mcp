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
check over DNS. Those are the realistic ways a "no egress" claim stops being true, and they all end
at one place — `socket.socket.connect`.

**What it deliberately does not touch.** Serving is `bind`/`listen`/`accept`, which are different
calls, so an armed process still answers requests normally. Loopback stays open: a sidecar, a
health probe against ourselves, and the in-process test client all live there. Non-`AF_INET`
families (Unix sockets) pass, because they cannot leave the host.

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
    """
    bare = host.strip("[]").lower()
    if bare in {"localhost", ""}:
        return True
    if bare.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(bare.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _host_of(address: Any) -> str | None:
    """The destination host of a `connect()` argument, or `None` when the family cannot leave.

    `AF_INET`/`AF_INET6` pass a tuple whose first element is the host. Everything else — a Unix
    socket path (a `str`/`bytes`), an `AF_NETLINK` tuple of ints — cannot reach another machine,
    so it is not this guard's business.
    """
    if isinstance(address, (str, bytes, bytearray)):
        return None
    if isinstance(address, Sequence) and address and isinstance(address[0], str):
        return address[0]
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

    socket.socket.connect = connect  # type: ignore[method-assign,assignment]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign,assignment]
    _armed = True


def disarm() -> None:
    """Restore the unguarded socket methods. For tests of the guard itself, and nothing else."""
    global _armed, _allowed
    socket.socket.connect = _original_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = _original_connect_ex  # type: ignore[method-assign]
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
