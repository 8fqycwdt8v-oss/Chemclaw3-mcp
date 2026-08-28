"""The three routes `egress.py` says walk past it, asserted as measurements rather than as prose.

`egress.py`'s docstring names four channels that are "outside any in-process patch by
construction" — a child process, a `ctypes` call straight into `libc`, the private C type
`_socket.socket`, and any syscall from a compiled extension — and hands them to `make offline-run`,
which takes the network namespace away instead of asking Python nicely.

**Nothing checked any of that.** `test_egress.py` is thorough about what the guard *does* catch;
the escapes were a paragraph. A paragraph is the shape of claim this repository keeps writing
decisions about, and it is worse here than usual in both directions: if a route the docstring
concedes were in fact caught, an operator would be relying on `offline-run` for a hole that is not
there, and if a route it does not mention escapes, the security argument has a gap nobody wrote
down.

So each is driven with the guard armed and the assertion is the same in all three: **the guard is
not consulted** — no `EgressForbidden`, and `chemclaw_mcp_egress_refused_total` does not move. That
is the honest form, and it is also the only form that holds in both lanes: with a network the
connect succeeds, and under `make offline-run` it fails with an ordinary `ENETUNREACH`. Either way
the guard never saw it, which is the property.

Measured on this tree, 2026-08-28, with the guard armed and `MCP_EGRESS_ALLOW` empty:

- `ctypes.CDLL("libc.so.6").connect(fd, sockaddr_in(203.0.113.10:443))` returned **rc=0** — a
  completed TCP connection.
- `subprocess` running `socket.create_connection` printed `CONNECTED`.
- `_socket.socket().connect(("203.0.113.10", 443))` connected.

The same address through `socket.socket` raises `EgressForbidden`, which is the control each test
asserts beside its escape so that "the guard did not fire" cannot be satisfied by a guard that
never fires.
"""

from __future__ import annotations

import ctypes
import socket
import struct
import subprocess
import sys
from collections.abc import Iterator

import pytest
from mcp_server_kit import egress
from mcp_server_kit.metrics import EGRESS_REFUSED

# TEST-NET-3 (RFC 5737). Nothing legitimate listens there, so a test that reached it would be
# reporting a proxy rather than a hole — and the assertions below never require it to be reachable.
HOST = "203.0.113.10"
PORT = 443
# Linux's `SOCK_NONBLOCK`, so the raw `libc` connect returns at once instead of waiting out a
# kernel timeout on a network where TEST-NET-3 is blackholed.
SOCK_NONBLOCK = 0o4000


@pytest.fixture(autouse=True)
def guard_is_armed_and_stays_armed() -> Iterator[None]:
    """Every test here needs the guard on; none of them may leave it off."""
    if not egress.armed():
        egress.arm()
    yield
    if not egress.armed():
        egress.arm()


def _refusals() -> float:
    """The counter an operator alerts on, read straight off the metric."""
    return float(EGRESS_REFUSED._value.get())


def test_the_guard_still_bites_on_the_ordinary_route() -> None:
    """The control. Without it, "the guard did not fire" is satisfied by a disarmed guard."""
    before = _refusals()
    with (
        pytest.raises(egress.EgressForbidden),
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client,
    ):
        client.connect((HOST, PORT))
    assert _refusals() == before + 1


def test_a_child_process_is_outside_the_guard() -> None:
    """A fresh interpreter never imports `mcp_server_kit`, so nothing arms anything in it.

    This is the escape that matters most in practice, because it is the one a server legitimately
    uses: `servers/calc` launches `xtb` and CREST, and `servers/pyexec` runs a caller's own program.
    Neither is covered by anything in this module — `servers/pyexec/engine/runner.py` re-disables
    the network inside its own child for exactly that reason, and the NetworkPolicy is what covers
    a binary that is not Python at all.
    """
    before = _refusals()
    program = (
        "import socket\n"
        "s = socket.socket(); s.settimeout(1)\n"
        "try:\n"
        f"    s.connect(({HOST!r}, {PORT})); print('reached')\n"
        "except Exception as exc:\n"
        "    print(type(exc).__name__)\n"
    )
    # Generous, because the deadline is not the property under test: a loaded CI box paying for
    # an interpreter start is not evidence about the guard, and a flaky assertion about a
    # security control is worse than a slow one.
    finished = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=300
    )
    said = finished.stdout.strip()
    assert said and "EgressForbidden" not in said, (
        f"a child process reported {said!r}; if the guard now reaches into subprocesses, "
        "`egress.py`'s docstring and this test are both out of date"
    )
    assert _refusals() == before, "the parent's counter moved for a call it never saw"


def test_a_ctypes_call_into_libc_is_outside_the_guard() -> None:
    """`arm()` rebinds Python methods; a direct `libc.connect` never reaches Python at all.

    Measured on this tree: `rc=0`, a completed connection, with the guard armed and the allowlist
    empty. The socket is opened non-blocking so this stays fast where TEST-NET-3 is blackholed —
    an `EINPROGRESS` is as much of an escape as a completed connection, because either way `_check`
    was never called.
    """
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
    except OSError:  # pragma: no cover - a platform this fleet does not ship on
        pytest.skip("no glibc to call into")
    before = _refusals()
    fd = libc.socket(socket.AF_INET, socket.SOCK_STREAM | SOCK_NONBLOCK, 0)
    assert fd >= 0, "could not open a raw socket to probe with"
    try:
        # `sockaddr_in`: the family in host byte order, the port in network order, then the
        # address. Packing the family big-endian yields `EAFNOSUPPORT` and a test that passes for
        # the wrong reason.
        sockaddr = (
            struct.pack("H", socket.AF_INET)
            + struct.pack("!H", PORT)
            + socket.inet_aton(HOST)
            + b"\x00" * 8
        )
        libc.connect(fd, sockaddr, len(sockaddr))
    finally:
        libc.close(fd)
    assert _refusals() == before, (
        "a `ctypes` call into libc was counted by the guard; that would mean `arm()` now reaches "
        "below Python, and `egress.py`'s docstring needs rewriting"
    )


def test_the_private_c_socket_type_is_outside_the_guard() -> None:
    """`socket.socket` subclasses `_socket.socket`, and `arm()` rebinds only the subclass.

    Measured: a real TCP connection completed. `no_egress.py` lists `_socket` in
    `FORBIDDEN_MODULES` precisely because the static scan is the only in-repo layer that can see
    the import — which is asserted in `test_no_egress.py`, not repeated here.
    """
    import _socket

    before = _refusals()
    raw = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    raw.settimeout(1.0)
    try:
        raw.connect((HOST, PORT))
    except egress.EgressForbidden as refused:  # pragma: no cover - the property under test
        pytest.fail(f"the private C type is guarded after all: {refused}")
    except OSError:
        pass  # No route to TEST-NET-3 — an ordinary network error, not the guard.
    finally:
        raw.close()
    assert _refusals() == before, "the guard counted a call it cannot have intercepted"


def test_the_private_c_socket_type_cannot_be_patched_and_that_is_why_it_escapes() -> None:
    """The reason for the escape above, asserted rather than asserted-about.

    `egress.py` says the C type "cannot be monkeypatched". If a future CPython made it a heap type,
    the largest documented hole in this guard could simply be closed — so the claim is worth a test
    that goes red on the day it stops being true rather than a sentence nobody re-derives.
    """
    import _socket

    with pytest.raises(TypeError, match="immutable type"):
        _socket.socket.connect = None  # type: ignore[misc]
