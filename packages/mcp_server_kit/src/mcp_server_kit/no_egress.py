"""The static half of the no-egress rule: our own code imports no way to call out.

`egress.py` catches what a dependency does at runtime; this catches what *we* write, in review.
AST-based, so `import x as y`, `from x import y`, `__import__("x")` and `import_module("x")` read
the same, and module names or hosts split across literals (`"gr" + "pc"`) are folded.

- A dynamic import whose name is computed from a value is reported by `computed_imports` and must
  be justified by its server's test, by scope, in both directions.
- Addresses assembled at runtime (f-strings, formats, decoded blobs) are out of reach; this is a
  review-time control, not a boundary.
- `socket`, `_socket` (the C type the runtime guard cannot patch) and `grpc` (a compiled
  transport) are forbidden in first-party code. `ctypes` is deliberately not: `pyexec`'s sandbox
  uses it for `prctl`, and `make offline-run` covers it.
- `exempt` is only for a file that runs in a child process and imports a network module in order
  to disable it, and its server owes a test proving that (`servers/pyexec/tests/test_no_egress.py`).
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from collections.abc import Iterable, Mapping
from pathlib import Path

__all__ = [
    "FORBIDDEN_MODULES",
    "assert_no_egress_sources",
    "computed_imports",
    "host_literals",
    "network_imports",
]

FORBIDDEN_MODULES = frozenset(
    {
        "aiohttp",
        "boto3",
        "ftplib",
        "grpc",
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

# The two ways to import a module by name at runtime, matched on the called name so any alias reads
# the same. Only a literal argument is resolved; a computed one goes to `computed_imports`.
_DYNAMIC_IMPORTS = frozenset({"__import__", "import_module"})


def _is_forbidden(module: str) -> bool:
    """Whether `module` — or a package it lives under — is one of the forbidden roots."""
    parts = module.split(".")
    return any(".".join(parts[: i + 1]) in FORBIDDEN_MODULES for i in range(len(parts)))


def _dynamic_import_name(node: ast.Call) -> ast.expr | None:
    """The expression a `__import__(...)` or `import_module(...)` call names its module with.

    The first positional argument or the `name=` keyword; `None` when `node` is neither call.
    """
    func = node.func
    if isinstance(func, ast.Attribute):
        called = func.attr
    elif isinstance(func, ast.Name):
        called = func.id
    else:
        return None
    if called not in _DYNAMIC_IMPORTS:
        return None
    if node.args:
        return node.args[0]
    return next((keyword.value for keyword in node.keywords if keyword.arg == "name"), None)


def _dynamic_import_target(node: ast.Call) -> str | None:
    """The module a `__import__("x")` or `import_module("x")` call names, when it is a literal.

    Folded through `_constant_string`, so a name split across literals is still read.
    """
    name = _dynamic_import_name(node)
    return None if name is None else _constant_string(name)


def computed_imports(source: Path) -> list[tuple[str, int]]:
    """Every dynamic import in `source` whose module name is computed from a value.

    Reported by the enclosing function or class (`"<module>"` at top level), since a justification
    names a scope, which outlives line numbers.

    Returns:
        `(scope, line)` for each such call, in source order.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    found: list[tuple[str, int]] = []

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                inner = child.name if scope == "<module>" else f"{scope}.{child.name}"
            if isinstance(child, ast.Call):
                name = _dynamic_import_name(child)
                if name is not None and _constant_string(name) is None:
                    found.append((scope, child.lineno))
            visit(child, inner)

    visit(tree, "<module>")
    return sorted(found, key=lambda site: site[1])


def network_imports(source: Path) -> list[str]:
    """Every forbidden module `source` imports, however the import is spelled.

    Statements plus `__import__`/`import_module` with a literal name; computed names are
    `computed_imports`' to report.
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

    Tokenized, so a `#` inside a string literal does not start one.
    """
    tokens = tokenize.generate_tokens(io.StringIO(text).readline)
    return "\n".join(token.string for token in tokens if token.type == tokenize.COMMENT)


def _static_strings(node: ast.AST) -> list[str]:
    """Every string in `node` a static reader can evaluate, each one folded whole.

    Top-down, so a folded `+` chain is reported once as the address it spells.
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

    A hostname in a tool module is either an intended call or documentation that belongs in a
    README;
    `dataset.json`'s `retrieved_from` is the sanctioned home for provenance. Comments come from the
    tokenizer and strings from the AST, folded so split or implicitly concatenated literals are one
    address. Runtime-assembled hosts are invisible by construction.

    Returns:
        The matches, deduplicated and sorted.
    """
    text = source.read_text(encoding="utf-8")
    found = set(_URL.findall(_comments(text)))
    for value in _static_strings(ast.parse(text, filename=str(source))):
        found.update(_URL.findall(value))
    return sorted(found)


def assert_no_egress_sources(
    *roots: Path,
    exempt: Iterable[Path] = (),
    justified_imports: Mapping[tuple[Path, str], str] | None = None,
) -> None:
    """Assert no `.py` file under `roots` imports a network client or names a remote host.

    Args:
        roots: Package directories to scan — a server passes its own `src/<package>`.
        exempt: Files to skip: only a file that runs in a child process and imports a network module
            to disable it, with a server test proving that.
        justified_imports: Computed dynamic imports keyed by `(file, scope)` as `computed_imports`
            reports them, each mapped to why it cannot load a network client. Held both ways: an
            unjustified site fails, as does a stale or blank justification.

    Raises:
        AssertionError: naming the file and what was found in it.
    """
    skipped = {path.resolve() for path in exempt}
    argued = {
        (path.resolve(), scope): reason
        for (path, scope), reason in (justified_imports or {}).items()
    }
    seen: set[tuple[Path, str]] = set()
    offences: list[str] = []
    for root in roots:
        for source in sorted(root.rglob("*.py")):
            if source.resolve() in skipped:
                continue
            for module in network_imports(source):
                offences.append(f"{source}: imports {module}")
            for host in host_literals(source):
                offences.append(f"{source}: names remote host {host}")
            for scope, line in computed_imports(source):
                key = (source.resolve(), scope)
                seen.add(key)
                if key not in argued:
                    offences.append(
                        f"{source}:{line}: imports a module named by a value in `{scope}`, which "
                        "no static reader can resolve; justify it by passing "
                        f"justified_imports={{(<this file>, {scope!r}): '<why it cannot load a "
                        "network client>'}"
                    )
    for (path, scope), reason in sorted(argued.items()):
        if (path, scope) not in seen:
            offences.append(
                f"{path}: `{scope}` is justified as a computed import and holds none; delete the "
                "justification, because one that outlived its site reads as a live argument"
            )
        elif not reason.strip():
            offences.append(f"{path}: `{scope}`'s computed import is justified with a blank reason")
    assert not offences, (
        "servers in this repository answer from vendored data and never call out:\n  "
        + "\n  ".join(offences)
    )
