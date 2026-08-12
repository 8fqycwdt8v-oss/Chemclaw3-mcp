"""Run the suite inside a network namespace with no route off the host.

The strongest form of the no-egress claim, and the only one that does not trust our own code: the
guard, the AST scan and the NetworkPolicy are all assertions *about* the servers, while this simply
takes the network away and checks that every answer is unchanged. If a server were quietly reaching
a host, it would fail here and nowhere else.

Run it as `make offline-run`, which is `unshare --net -- python scripts/offline_check.py`.

**Loopback has to be brought up by hand.** A fresh network namespace starts with `lo` DOWN, so
without this the run fails on the server tests for a reason that has nothing to do with egress —
which would teach whoever hit it that the check is noise. `iproute2` is not in the slim images this
repository builds on, so the interface flags are set through the ioctl `ip link set lo up` would
have used.
"""

from __future__ import annotations

import fcntl
import os
import socket
import struct
import sys

SIOCSIFFLAGS = 0x8914
IFF_UP = 0x1
IFF_RUNNING = 0x40


def bring_loopback_up() -> None:
    """Set `lo` UP in the current network namespace."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        request = struct.pack("16sh", b"lo", IFF_UP | IFF_RUNNING)
        fcntl.ioctl(control.fileno(), SIOCSIFFLAGS, request)


def main() -> int:
    """Bring loopback up, then hand over to pytest with whatever arguments were passed."""
    try:
        bring_loopback_up()
    except OSError as error:  # pragma: no cover - depends on the runner's capabilities
        print(
            f"could not bring loopback up ({error}); are we in an unshared namespace?",
            file=sys.stderr,
        )
        return 2
    args = sys.argv[1:] or ["-q"]
    os.execvp("pytest", ["pytest", *args])


if __name__ == "__main__":
    raise SystemExit(main())
