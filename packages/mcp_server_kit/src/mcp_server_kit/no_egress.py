"""The static half of the no-egress rule: our own code imports no way to call out.

The runtime guard (`egress.py`) catches what a dependency does. This catches what *we* do, and it
catches it in review rather than in production — a `requests.get` added to a tool fails CI on the
pull request that adds it, which is the moment it is cheap to argue about.

**AST, not grep.** `import httpx as h`, `from requests import get`, and a `__import__("urllib")`
all read differently as text and identically as a tree. A grep-based version of this check passes
on the first two, which makes it worse than no check: it reports a clean scan.

`socket` is on the list even though it is stdlib and the guard patches it, because a server here has
no legitimate reason to hold one — and a module that imports it can also un-patch the guard.

**`exempt` exists for exactly one shape, and it is narrower than it looks.** That last sentence is a
statement about code running *in the server process*. `servers/pyexec` ships a file that never
does: `engine/runner.py` is executed as a script in a disposable child, and it imports `socket` in
order to replace `connect` with a refusal — the opposite of egress, and unreachable from the process
this scan protects. The alternative was measured and rejected: arming this kit's own guard in the
child means importing `mcp_server_kit`, which costs **730 ms** against a 13 ms bare interpreter,
paid on every analysis. So a server may name files whose network import is the disabling one, and
owes a test proving that is all it is — `servers/pyexec/tests/test_no_egress.py` is the pattern.
Exempting anything else is a decision to argue in the pull request that does it.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from pathlib import Path

__all__ = ["FORBIDDEN_MODULES", "assert_no_egress_sources", "host_literals", "network_imports"]

FORBIDDEN_MODULES = frozenset(
    {
        "aiohttp",
        "boto3",
        "ftplib",
        "http.client",
        "httpcore",
        "httplib2",
        "httpx",
        "requests",
        "smtplib",
        "socket",
        "telnetlib",
        "urllib.request",
        "urllib3",
        "websockets",
    }
)

# A URL to somewhere else. Loopback is exempt: a docstring showing how to curl the server's own
# `/healthz` is documentation, not egress.
_URL = re.compile(r"https?://(?!127\.0\.0\.1|localhost|\[::1\])[A-Za-z0-9.-]+", re.IGNORECASE)


def _is_forbidden(module: str) -> bool:
    """Whether `module` — or a package it lives under — is one of the forbidden roots."""
    parts = module.split(".")
    return any(".".join(parts[: i + 1]) in FORBIDDEN_MODULES for i in range(len(parts)))


def network_imports(source: Path) -> list[str]:
    """Every forbidden module `source` imports, however the import is spelled."""
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names if _is_forbidden(alias.name))
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if _is_forbidden(node.module):
                found.append(node.module)
            else:
                found.extend(
                    f"{node.module}.{alias.name}"
                    for alias in node.names
                    if _is_forbidden(f"{node.module}.{alias.name}")
                )
    return found


def host_literals(source: Path) -> list[str]:
    """Every non-loopback URL literal in `source`, including the ones inside docstrings.

    Deliberately includes comments and docstrings. A hostname written down in a tool module is
    either an address something intends to call, or documentation that belongs in the server's
    README where a reader will actually find it — `dataset.json`'s `retrieved_from` is the one
    sanctioned home for "where a human got this file".
    """
    return _URL.findall(source.read_text(encoding="utf-8"))


def assert_no_egress_sources(*roots: Path, exempt: Iterable[Path] = ()) -> None:
    """Assert no `.py` file under `roots` imports a network client or names a remote host.

    Args:
        roots: Package directories to scan — a server passes its own `src/<package>`.
        exempt: Files to skip, resolved before comparison. For the one shape described in this
            module's docstring: a file that runs in a child process and imports a network module in
            order to disable it. A server that passes anything here owes a test proving that is what
            the file does; an exemption without one is an unchecked claim, which is the failure this
            whole scan exists to prevent.

    Raises:
        AssertionError: naming the file and what was found in it.
    """
    skipped = {path.resolve() for path in exempt}
    offences: list[str] = []
    for root in roots:
        for source in sorted(root.rglob("*.py")):
            if source.resolve() in skipped:
                continue
            for module in network_imports(source):
                offences.append(f"{source}: imports {module}")
            for host in host_literals(source):
                offences.append(f"{source}: names remote host {host}")
    assert not offences, (
        "servers in this repository answer from vendored data and never call out:\n  "
        + "\n  ".join(offences)
    )
