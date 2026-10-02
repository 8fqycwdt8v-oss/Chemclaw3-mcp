"""The DNS-rebinding guard on `/mcp`: kept on, and told which names this pod is addressed by.

**What upstream does, and why every in-cluster call was refused.** `FastMCP(name)` (mcp 1.29)
switches its DNS-rebinding protection on whenever its own `host` setting is loopback — which it
always is for `FastMCP("x")`, the default, *however uvicorn is later bound* — and then admits only a
`Host` header of `127.0.0.1:*`, `localhost:*` or `[::1]:*`. Every caller that dials a Service name
(`chemclaw-mcp-safety` on 8859, the short-name address Chemclaw3's chart ships) sends
`Host: chemclaw-mcp-safety:8859` and is answered `421 Misdirected Request` before the bearer check,
the session manager or a tool ever sees it. Measured on a kind cluster: every server in this fleet,
every tool, while `/healthz` — a plain route outside the MCP app — stayed green and so did every
probe.

**What this does instead.** The guard stays on, with the loopback defaults upstream would have
used, and `MCP_ALLOWED_HOSTS` *adds* to them: a comma-separated list of `host:port` or `host:*`
entries, each one a name this pod is legitimately addressed by. Unset (or empty), the settings are
exactly the loopback-only ones upstream builds, so a dev server on `127.0.0.1` behaves as it did.
Every shipped `deploy/deployment.yaml` sets it to its own Service's `name:port`.
`D-2026-10-02-the-rebinding-guard-stays-on-and-is-told-the-service-name` has the alternative weighed
(switching the guard off) and why it was not taken.

**An entry is refused at import, naming the variable and the entry**, for the same reason
`limits.env_bound` refuses a bound there: the realistic mistake is a URL pasted where a host was
wanted, or a `*` written to make the 421 go away, and either one would otherwise start a pod that
is either still refusing everything or no longer guarding anything — with `/healthz` green in both
cases. A wildcard *host* is refused outright: admitting any `Host` is switching the guard off with
extra steps, and that is a decision for a new record rather than an environment variable.
"""

from __future__ import annotations

import ipaddress
import os
import re

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

ALLOWED_HOSTS_ENV = "MCP_ALLOWED_HOSTS"

#: What upstream's `FastMCP.__init__` admits for a loopback-configured server, transcribed so that
#: "unset" means exactly that. `tests/test_rebinding.py` holds the transcription against a freshly
#: constructed `FastMCP`, so an upstream change to its defaults turns a test red rather than
#: silently widening or narrowing this fleet's.
LOOPBACK_HOSTS: tuple[str, ...] = ("127.0.0.1:*", "localhost:*", "[::1]:*")
LOOPBACK_ORIGINS: tuple[str, ...] = (
    "http://127.0.0.1:*",
    "http://localhost:*",
    "http://[::1]:*",
)

# A DNS name or a dotted IPv4 address, lower case. Upper case is refused rather than folded:
# upstream compares the `Host` header byte for byte, so a folded entry would still not match the
# header the operator was thinking of, and a refusal says so where folding would say nothing.
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

    `None`, an empty string and whitespace are all "unset", matching how a Kubernetes `env:` entry
    with no value arrives. Inside a non-empty value, an empty entry (`a,,b`, a trailing comma) is
    refused rather than skipped: it is a typo in a list somebody meant, and the entry it was meant
    to be is missing.

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

    The origins follow the hosts — `http://<entry>` for each — because upstream checks `Origin`
    whenever a request carries one, and a page served from an admitted host is the same origin as
    the server it is talking to. A server-to-server caller (Chemclaw3, `httpx`) sends no `Origin`
    at all, which upstream admits.
    """
    extra = parse_allowed_hosts(raw)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[*LOOPBACK_HOSTS, *extra],
        allowed_origins=[*LOOPBACK_ORIGINS, *(f"http://{entry}" for entry in extra)],
    )


def apply_allowed_hosts(server: FastMCP) -> None:
    """Install the guard's settings on `server`, from `MCP_ALLOWED_HOSTS`, before its app is built.

    Must run before `server.streamable_http_app()`, which is where upstream reads the setting.
    It **replaces** whatever the `FastMCP` was constructed with rather than merging into it: a
    `FastMCP(host="0.0.0.0")` would otherwise arrive here with the guard off, and one place deciding
    the posture for every server is the reason this lives in the kit.
    """
    server.settings.transport_security = transport_security(os.environ.get(ALLOWED_HOSTS_ENV))
