"""The static half of the no-egress rule: our own code imports no way to call out.

The runtime guard (`egress.py`) catches what a dependency does. This catches what *we* do, and it
catches it in review rather than in production — a `requests.get` added to a tool fails CI on the
pull request that adds it, which is the moment it is cheap to argue about.

**AST, not grep.** `import httpx as h`, `from requests import get`, and a `__import__("urllib")`
all read differently as text and identically as a tree. A grep-based version of this check passes
on the first two, which makes it worse than no check: it reports a clean scan.

**That claim outran the code, and the third spelling was the one it named.** `network_imports`
walked `ast.Import` and `ast.ImportFrom` only, and a `__import__(...)` is an `ast.Call` — measured,
a file whose whole body was `h = __import__("httpx")` scanned clean, as did
`importlib.import_module("socket")`, which is the module the list carries *because* "a module that
imports it can also un-patch the guard". Both spellings are covered now, and so is a host written
as `"http://" + "example" + ".com"`, which the text regex read as three harmless fragments.

**What is deliberately still not covered, so that nobody has to infer it from a clean scan:** an
import whose module name is computed (`importlib.import_module(name)`), and any address assembled
at runtime — an f-string, a `%` format, a `"".join`, a decoded blob. The first has a real caller in
this fleet (`servers/rxnpredict` loads its optional predictor plug-ins that way), and flagging the
shape would make correct code fail while teaching the next reader to reach for `exempt`. The
second is unbounded by construction: no static reader evaluates arbitrary expressions. **This is a
review-time control against what somebody writes down, not a boundary** — what a computed import
or a computed address actually does is `egress.py`'s job at runtime and `make offline-run`'s when
the call leaves Python entirely.

`socket` is on the list even though it is stdlib and the guard patches it, because a server here has
no legitimate reason to hold one — and a module that imports it can also un-patch the guard.

**`_socket` is on the list too, and it is the one the runtime guard cannot reach.** `socket.socket`
subclasses the C type `_socket.socket`, and `egress.arm()` rebinds the Python subclass — a
`_socket.socket().connect(...)` goes straight to the C method the guard never touched (measured: a
real TCP connection completed with the guard armed). The C type cannot be monkeypatched, so this
static scan is the *only* in-repo layer that can see the import; `make offline-run` is the only
runtime one. A server has no more reason to hold the private C socket than the public wrapper.

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
import io
import re
import tokenize
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
        "_socket",
        "telnetlib",
        "urllib.request",
        "urllib3",
        "websockets",
    }
)

# A URL to somewhere else. Loopback is exempt: a docstring showing how to curl the server's own
# `/healthz` is documentation, not egress.
_URL = re.compile(r"https?://(?!127\.0\.0\.1|localhost|\[::1\])[A-Za-z0-9.-]+", re.IGNORECASE)

# The two ways to import a module by name at runtime. Matched on the *called name* rather than on
# the object it hangs off, so `importlib.import_module`, a `from importlib import import_module`
# and a rebound alias all read the same — the alternative is tracking assignments, which is a
# different program. Only a literal first argument is resolved; see this module's docstring for why
# a computed one is left to the runtime guard.
_DYNAMIC_IMPORTS = frozenset({"__import__", "import_module"})


def _is_forbidden(module: str) -> bool:
    """Whether `module` — or a package it lives under — is one of the forbidden roots."""
    parts = module.split(".")
    return any(".".join(parts[: i + 1]) in FORBIDDEN_MODULES for i in range(len(parts)))


def _dynamic_import_target(node: ast.Call) -> str | None:
    """The module a `__import__("x")` or `import_module("x")` call names, when it is a literal."""
    func = node.func
    if isinstance(func, ast.Attribute):
        called = func.attr
    elif isinstance(func, ast.Name):
        called = func.id
    else:
        return None
    if called not in _DYNAMIC_IMPORTS or not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def network_imports(source: Path) -> list[str]:
    """Every forbidden module `source` imports, however the import is spelled.

    Covers the statement forms (`import x`, `from x import y`) and the two runtime forms with a
    literal name (`__import__("x")`, `import_module("x")`). A computed name is not an offence —
    the module docstring says why, and says what covers it instead.
    """
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
        elif isinstance(node, ast.Call):
            target = _dynamic_import_target(node)
            if target is not None and _is_forbidden(target):
                found.append(target)
    return found


def _comments(text: str) -> str:
    """Every comment in `text`, joined — the one place a URL hides that the AST cannot see.

    Tokenized rather than matched, so a `#` inside a string literal is not read as starting one.
    """
    tokens = tokenize.generate_tokens(io.StringIO(text).readline)
    return "\n".join(token.string for token in tokens if token.type == tokenize.COMMENT)


def _static_strings(node: ast.AST) -> list[str]:
    """Every string in `node` a static reader can evaluate, each one folded whole.

    Top-down rather than `ast.walk`, so a folded `+` chain is reported once as the address it
    spells instead of once per partial sum — `"http://" + "example" + ".com"` is one host, not
    also `http://example`.
    """
    folded = _constant_string(node)
    if folded is not None:
        return [folded]
    return [found for child in ast.iter_child_nodes(node) for found in _static_strings(child)]


def _constant_string(node: ast.AST) -> str | None:
    """`node` as the string it is, if it is one — a literal, or a `+` chain of literals."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _constant_string(node.left)
        right = _constant_string(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def host_literals(source: Path) -> list[str]:
    """Every non-loopback URL `source` spells out, including the ones inside docstrings.

    Deliberately includes comments and docstrings. A hostname written down in a tool module is
    either an address something intends to call, or documentation that belongs in the server's
    README where a reader will actually find it — `dataset.json`'s `retrieved_from` is the one
    sanctioned home for "where a human got this file".

    Two passes that partition the file rather than overlap on it, because in valid Python a URL
    can only be in a comment or in a string: the tokenizer supplies the comments, and the AST
    supplies every string — each one folded, so `"http://" + "example" + ".com"` and Python's
    implicit adjacency (the same split without an operator) are the single address they spell
    rather than three harmless fragments. Both were clean under the regex-over-raw-text this
    replaces, and that pass had the mirror-image flaw: reading a split loopback URL one fragment
    at a time, `"http://127." + "0.0.1"` reported `http://127.` as a remote host, because a
    truncated address does not satisfy the loopback exemption.

    A host assembled from anything that is not a literal — an f-string, a `%` format, a decoded
    blob — is visible to neither, by construction; see the module docstring.

    Returns:
        The matches, deduplicated and sorted.
    """
    text = source.read_text(encoding="utf-8")
    found = set(_URL.findall(_comments(text)))
    for value in _static_strings(ast.parse(text, filename=str(source))):
        found.update(_URL.findall(value))
    return sorted(found)


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
