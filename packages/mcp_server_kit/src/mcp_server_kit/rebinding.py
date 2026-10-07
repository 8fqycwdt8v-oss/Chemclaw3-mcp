"""The DNS-rebinding guard on `/mcp`: kept on, and told which names this pod is addressed by.

Upstream's `FastMCP("x")` enables the guard admitting only loopback `Host` headers, so a caller
dialling a Service name gets `421`. This keeps the guard on with the loopback defaults and adds
`MCP_ALLOWED_HOSTS` — comma-separated `host:port` or `host:*` entries; every shipped Deployment
sets its own Service's `name:port`. Unset means exactly upstream's loopback-only behaviour.

An invalid entry (a URL, a wildcard host, no port) is refused at import, naming it: a wildcard host
would switch the guard off.
"""

from __future__ import annotations

import ipaddress
import os
import re

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

ALLOWED_HOSTS_ENV = "MCP_ALLOWED_HOSTS"

#: Upstream's loopback defaults, transcribed so "unset" means exactly that;
#: `tests/test_rebinding.py` holds them against a fresh `FastMCP`.
LOOPBACK_HOSTS: tuple[str, ...] = ("127.0.0.1:*", "localhost:*", "[::1]:*")
LOOPBACK_ORIGINS: tuple[str, ...] = (
    "http://127.0.0.1:*",
    "http://localhost:*",
    "http://[::1]:*",
)

# A DNS name or dotted IPv4 address, lower case. Upper case is refused, not folded: upstream
# compares the `Host` header byte for byte.
_HOSTNAME = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*")


def _refuse(entry: str, why: str) -> ValueError:
    return ValueError(
        f"{ALLOWED_HOSTS_ENV} entry {entry!r} {why}. Each entry is `host:port` or `host:*` — the "
        f"`Host` header a caller sends, e.g. `chemclaw-mcp-safety:8859` — separated by commas; "
        f"unset {ALLOWED_HOSTS_ENV} to admit loopback only."
    )


def _validate(entry: str) -> str:
    """Return `entry` if it is a `host:port` or `host:*` the guard can match, or raise."""
    if any(character.isspace() for character in entry):
        raise _refuse(entry, "contains whitespace")
    if "://" in entry or "/" in entry or "@" in entry:
        raise _refuse(entry, "is a URL, not a host: drop the scheme, any path and any userinfo")
    if entry.startswith("["):
        host, bracket, port = entry[1:].partition("]")
        if not bracket or not port.startswith(":"):
            raise _refuse(entry, "is a bracketed IPv6 address with no `]:port` after it")
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise _refuse(entry, "is not a valid IPv6 address inside the brackets") from None
        port = port[1:]
    else:
        host, colon, port = entry.rpartition(":")
        if "*" in (host if colon else entry):
            raise _refuse(
                entry,
                "has a wildcard host; admitting any `Host` is turning the DNS-rebinding guard off, "
                "which is not something this variable does",
            )
        if not colon:
            raise _refuse(
                entry,
                "has no port; a caller's `Host` header carries the port it dialled, so write "
                "`host:port`, or `host:*` for any port",
            )
        if ":" in host:
            raise _refuse(entry, "looks like an unbracketed IPv6 address; write `[addr]:port`")
        if not _HOSTNAME.fullmatch(host):
            raise _refuse(
                entry,
                "is not a lower-case DNS name or IPv4 address (the guard compares the `Host` "
                "header byte for byte)",
            )
    if port != "*" and not (port.isdigit() and port.isascii() and 0 < int(port) < 65536):
        raise _refuse(entry, "has a port that is neither `*` nor a number from 1 to 65535")
    return entry


def parse_allowed_hosts(raw: str | None) -> tuple[str, ...]:
    """The extra `Host` values `raw` admits, validated, in order, without duplicates.

    `None`, empty and whitespace mean unset. An empty entry inside a list (`a,,b`) is a typo and is
    refused.

    Raises:
        ValueError: An entry is empty, carries whitespace, is a URL, has a wildcard host, has no
            port, or does not parse as a host and port.
    """
    if raw is None or not raw.strip():
        return ()
    entries: list[str] = []
    for part in raw.split(","):
        entry = part.strip()
        if not entry:
            raise _refuse(part, "is empty (a doubled or trailing comma)")
        validated = _validate(entry)
        if validated not in entries:
            entries.append(validated)
    return tuple(entries)


def transport_security(raw: str | None) -> TransportSecuritySettings:
    """The settings `/mcp` is guarded with: always on, loopback plus what `raw` adds.

    Allowed origins are `http://<entry>` for each host; server-to-server callers send no `Origin`.
    """
    extra = parse_allowed_hosts(raw)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[*LOOPBACK_HOSTS, *extra],
        allowed_origins=[*LOOPBACK_ORIGINS, *(f"http://{entry}" for entry in extra)],
    )


def apply_allowed_hosts(server: FastMCP) -> None:
    """Install the guard's settings on `server`, from `MCP_ALLOWED_HOSTS`, before its app is built.

    Must run before `server.streamable_http_app()`, which reads them. Replaces, not merges, so a
    `FastMCP(host="0.0.0.0")` cannot arrive with the guard off.
    """
    server.settings.transport_security = transport_security(os.environ.get(ALLOWED_HOSTS_ENV))
