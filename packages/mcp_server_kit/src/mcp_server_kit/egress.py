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
along with `connect`/`connect_ex`. **A reverse lookup is the same round trip run backwards** —
`getnameinfo` and `gethostbyaddr` resolve an address to a name, and the *address* is then the covert
channel; both completed with the guard armed until they were patched too.

**What it deliberately does not touch, and what covers each instead.** Serving is
`bind`/`listen`/`accept`, which are different calls, so an armed process still answers requests
normally. Loopback stays open: a sidecar, a health probe against ourselves, and the in-process test
client all live there. Non-`AF_INET` families (Unix sockets) pass, because they cannot leave the
host. And four channels are outside any in-process patch by construction — a **child process**
(`subprocess`, `os.system`), a **`ctypes` call straight into `libc.connect`**, the private C type
**`_socket.socket`** (whose methods `arm()` rebinds only on the Python `socket.socket` subclass, so
a `_socket.socket().connect(...)` never sees the guard — caught statically by `no_egress.py`, which
lists `_socket`), and any syscall made from a compiled extension. Those are `make offline-run`'s
job: it takes the network namespace away,
which is the only layer that does not depend on the caller going through Python. `servers/pyexec`
is the server this matters most for, and its README states the same division — the child process
and its rlimits are the boundary there, not the guards inside the parent.

The allowlist is `MCP_EGRESS_ALLOW`, and it is **empty by default and empty in the shipped chart**.
It exists so a build-time ingestion step — the one sanctioned moment a dataset is fetched — can run
with the guard relaxed *outside* the serving image, not so a server can be talked into calling out.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import threading
from collections.abc import Iterable, Sequence
from typing import Any

from mcp_server_kit.metrics import EGRESS_GUARD_ARMED, EGRESS_REFUSED

__all__ = ["EgressForbidden", "allowed_hosts", "arm", "armed", "disarm"]

logger = logging.getLogger(__name__)


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
_original_getnameinfo = socket.getnameinfo
_original_gethostbyaddr = socket.gethostbyaddr

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
    rather than the two spellings people remember. A name is only loopback if it *is* exactly
    `localhost`: resolving an arbitrary name here would mean a DNS lookup, which is itself a call
    out, and a guard whose check leaks is not a guard.

    **A `.localhost` *suffix* is not loopback, and treating it as one was a bypass.** The rule used
    to exempt any name ending in `.localhost`, but the OS resolver decides what such a name points
    at — measured, `connect(("exfil.localhost", 80))` reached a public address given an
    `/etc/hosts` entry, and `getaddrinfo("<payload>.localhost")` was permitted, i.e. DNS
    exfiltration through a name the guard waved through. Only the exact string `localhost` and real
    loopback IP literals pass now. A site that genuinely serves a `.localhost` name adds it to
    `MCP_EGRESS_ALLOW`, the one sanctioned way to widen the guard.

    **The unspecified address counts too, and it started mattering when this guard grew to cover
    `getaddrinfo`.** `0.0.0.0` and `::` are what a container binds to, not somewhere to reach — and
    a `connect` to either lands on this machine anyway. CPython short-circuits a numeric bind
    address before it reaches `getaddrinfo`, so nothing observed this; a guard that refuses a
    server's own bind on some other path would be an outage rather than a control.
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


# Set while a refusal is being recorded, so the recording cannot record itself. Thread-local
# rather than a module flag: two threads refusing two different destinations at the same instant
# are two events, and a shared flag would drop one of them. See `_report`.
_reporting = threading.local()


def _report(host: str) -> None:
    """Count and log one refusal, unless this is the *recording* of a refusal calling back in.

    **The record is what re-entered the guard.** `logger.error` runs whatever handlers the
    deployment configured, and a network-backed one — `SysLogHandler`, `SocketHandler`, anything
    wired through uvicorn's `--log-config` — connects on emit. That connect is egress, so it lands
    back in `_check`, which counts it, logs it, and connects again. Measured with a `SocketHandler`
    pointed at an unreachable collector: **one** refused `connect` booked **83** on
    `chemclaw_mcp_egress_refused_total` and produced 83 log lines, 82 of them naming the *log
    server* rather than the destination that was actually refused — so the one line an operator
    needs was buried, and `rate(chemclaw_mcp_egress_refused_total) > 0`, the alert this counter
    exists for, fired at 83 times the real rate. The recursion itself ended in a `RecursionError`
    that logging's own error path swallowed, so nothing said any of this had happened.

    The inner connection is still **refused** — `_check` raises for it exactly as before, which is
    the guard doing its job. It is only the second, third and eighty-third *record* of one event
    that is dropped.
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

    **The refusal is logged and counted before it is raised, and that is not decoration.**
    `EgressForbidden` derives from `OSError` — the family libraries retry on — so what a refusal
    looks like from outside depends entirely on who catches it, and three real catchers in this
    fleet turn it into something else: `servers/calc/tools.py` catches `(OSError, SubprocessError)`
    and reports "could not resolve the xTB backend", `servers/rxnpredict/tools.py` gathers with
    `return_exceptions=True` so a transformer reaching for weights degrades the ensemble silently,
    and any library's own `except OSError: retry` swallows it whole. The fleet's central security
    promise was therefore enforceable and invisible: nothing recorded that it had ever fired.

    **The host and nothing else.** A destination is enough to name the library that tried to call
    out, which is what a stack trace ending here is for; the payload of a refused `sendto` is not
    this log line's business. `EGRESS_REFUSED` is unlabelled for the reason `metrics.py` gives —
    the host is attacker-influenced and unbounded, and a bare counter is all `rate(...) > 0` needs.

    **The recording is itself re-entrant, and `_report` is where that is handled** — a log handler
    that writes to the network connects, and that connect arrives back here. The refusal is
    unconditional either way; only the count and the line are guarded.
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

    def getnameinfo(sockaddr: Any, flags: Any) -> Any:
        # A reverse lookup: `sockaddr` is the `(address, port)` tuple `_host_of` already reads, and
        # the address it resolves is the covert channel. Loopback still passes, so a name for the
        # server's own socket resolves normally.
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


def arm_from_env() -> None:
    """Arm unless `MCP_EGRESS_GUARD` is explicitly `off`.

    Called from this package's `__init__`, so importing any part of a server arms it. The opt-out
    exists for the ingestion scripts and for debugging; it is not set in any shipped deployment,
    and `tests/test_egress.py` asserts the default is on.
    """
    if os.environ.get(_GUARD_ENV, "on").strip().lower() in {"off", "0", "false", "no"}:
        # Recorded rather than merely returned: a deployment that shipped `MCP_EGRESS_GUARD=off`
        # used to be visible only in a docstring, and the gauge is what makes "the guard is armed"
        # a fact a scrape can check instead of a claim a document makes.
        EGRESS_GUARD_ARMED.set(0)
        return
    arm()
