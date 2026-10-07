"""The child half of the sandbox: the only module that runs a caller's program.

Executed as a script by `sandbox.py` (`python -I -B runner.py <payload> <result>`). It must import
nothing from its own package: `-I` implies `-P`, so a sibling import would fail in the child.

Order of operations: read the payload; apply address-space, file-size and descriptor limits; patch
the socket module; apply `RLIMIT_CPU`/`RLIMIT_NPROC` measured from now (CPU is cumulative, NPROC
counts threads); run the program with restricted builtins. Nothing is pre-imported, so
`cpu_seconds` includes the program's own imports.

The import guard is a replaced `__import__` in the analysis namespace's builtins: a caller's
`import` resolves it from its own frame, a library's from the real `builtins`, so libraries keep
working and `sys.modules` needs no purging. The guard and the jailed `open` are ergonomics and
defence in depth, porous by construction. **The security boundary is the process and the
deployment**: process-group kill on a wall clock, hard rlimits, an allowlisted environment, an
undumpable parent, a per-call scratch directory, a rootless read-only container and an empty
`egress:` NetworkPolicy. `README.md` records what the boundary does not cover.
"""

from __future__ import annotations

import base64
import builtins
import contextlib
import io
import json
import os
import resource
import sys
import traceback
from collections.abc import Callable, Mapping, Sequence
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import ModuleType
from typing import Any

#: The filename a caller's program is compiled under, so a traceback names the sandbox rather than
#: `<string>` — which reads, to a chemist, as though something went wrong inside the tool.
ANALYSIS_FILENAME = "<analysis>"

#: Root module names an analysis may import. Everything else is refused by name; an allowlist
#: : because a name missing from it fails closed.
ALLOWED_IMPORTS: frozenset[str] = frozenset(
    {
        # The reason this server exists.
        "numpy",
        "pandas",
        "scipy",
        "rdkit",
        "matplotlib",
        "sympy",
        "sklearn",  # scikit-learn's import name; see pyproject.toml for the package name.
        "openbabel",
        # Some OpenBabel builds ship `pybel` as a standalone top-level module.
        "pybel",
        # Arithmetic and numbers.
        "math",
        "cmath",
        "statistics",
        "decimal",
        "fractions",
        "random",
        # Containers, iteration, and the functional toolbox.
        "collections",
        "itertools",
        "functools",
        "operator",
        "heapq",
        "bisect",
        "array",
        "copy",
        "enum",
        "dataclasses",
        "typing",
        "abc",
        # Text, structure and time.
        "re",
        "json",
        "csv",
        "string",
        "textwrap",
        "unicodedata",
        "datetime",
        "zoneinfo",
        "pprint",
        "base64",
        "binascii",
        "struct",
        "hashlib",
        "uuid",
        "warnings",
    }
)

#: Builtins removed from the namespace a program runs in: interactive ones, ones fatal to the
#: : runner, and `eval`/`exec`/`compile`, which would skip the guarded `__import__`. `__import__` and
#: : `open` are replaced rather than withheld; `__build_class__` stays so `class` works.
WITHHELD_BUILTINS: frozenset[str] = frozenset(
    {"input", "help", "breakpoint", "exit", "quit", "eval", "exec", "compile"}
)


class SandboxPathError(ValueError):
    """A program's `open()` call named a path outside its own scratch directory.

    Its own class so the chemist sees a sandbox refusal, not a deployment-looking `OSError`.
    """


def _make_guarded_open(jail: Path, opened: list[Any]) -> Callable[..., Any]:
    """Build `open()` for one run: jailed to `jail`, tracking every handle it returns in `opened`.

    The jail is pinned before the analysis runs and never re-derived from `Path.cwd()`: allowed
    modules expose `os` through their attributes (`uuid.os.chdir`), so a cwd-based check could be
    moved by the program. Every handle is recorded so `main()` can close them all before writing its
    own result; otherwise a program leaking descriptors exhausts `RLIMIT_NOFILE` and the runner
    cannot write a correct answer.
    """

    def _guarded_open(
        file: Any,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> Any:
        """`open()` for the analysis namespace: resolve against the pinned jail, then the real `open`.

        There is deliberately no `**kwargs`: forwarding `opener=` would let a program return a
        descriptor for any path while the checked name stays in the jail; `closefd` is dropped
        likewise. Paths are resolved with `Path.resolve()` (following `..` and symlinks) before the
        containment check, and an integer descriptor is refused outright. `os` and `pathlib` stay
        off `ALLOWED_IMPORTS` because their own file methods bypass this function. A library opening
        a path through its own `open` (`savefig`, `MolToMolFile`) is not covered; see `README.md`.
        """
        if isinstance(file, int):
            raise SandboxPathError(
                "open() does not accept a file descriptor in the analysis sandbox; pass a path"
            )
        candidate = Path(os.fsdecode(file))
        target = candidate if candidate.is_absolute() else jail / candidate
        resolved = target.resolve()
        try:
            resolved.relative_to(jail)
        except ValueError:
            raise SandboxPathError(
                f"{file!r} resolves outside the sandbox's scratch directory ({jail}); a program "
                "may only read and write files under its own working directory, and nothing "
                "written there survives the call"
            ) from None
        # The handle must outlive this call; `main()` closes it via `opened`.
        handle = builtins.open(resolved, mode, buffering, encoding, errors, newline)  # noqa: SIM115
        opened.append(handle)
        return handle

    # Named `open` so Python's own argument-binding errors read as the program's fault.
    _guarded_open.__name__ = "open"
    _guarded_open.__qualname__ = "open"
    return _guarded_open


class SandboxImportError(ImportError):
    """A program asked for a module the sandbox does not offer.

    Its own class so the message names the sandbox rather than reading as a broken deployment.
    """


def _guarded_import(
    name: str,
    # The two shadowed builtins are `__import__`'s own parameter names. Renaming them would be a
    # signature that no longer matches the one the `import` statement calls.
    globals: Mapping[str, object] | None = None,
    locals: Mapping[str, object] | None = None,
    fromlist: Sequence[str] = (),
    level: int = 0,
) -> ModuleType:
    """`__import__` for the analysis namespace: allowlist first, then the real machinery.

    The signature matches `builtins.__import__` positionally, as the `import` statement calls it.
    """
    root = name.split(".", 1)[0]
    if root not in ALLOWED_IMPORTS:
        raise SandboxImportError(
            f"{root!r} is not available in the analysis sandbox. Available: "
            f"{', '.join(sorted(ALLOWED_IMPORTS))}."
        )
    return builtins.__import__(name, globals, locals, fromlist, level)


# Resource limits.


def _set(which: int, value: int) -> None:
    """Set one rlimit's soft and hard bound, never above what we were given.

    The hard bound makes it stick (a soft limit can be raised back by the program); the `min` keeps
    it working unprivileged.
    """
    _, hard = resource.getrlimit(which)
    ceiling = value if hard == resource.RLIM_INFINITY else min(value, hard)
    resource.setrlimit(which, (ceiling, ceiling))


def _apply_static_limits(limits: dict[str, Any]) -> None:
    """The bounds that cannot break an import, applied before anything heavy is loaded."""
    _set(resource.RLIMIT_AS, int(limits["memory_bytes"]))
    _set(resource.RLIMIT_FSIZE, int(limits["file_bytes"]))
    _set(resource.RLIMIT_NOFILE, int(limits["open_files"]))


def _apply_runtime_limits(limits: dict[str, Any]) -> None:
    """CPU and process count, applied last because both are measured from now.

    The soft CPU bound raises `SIGXCPU`; the hard bound a second later is `SIGKILL`.
    """
    spent = resource.getrusage(resource.RUSAGE_SELF)
    budget = int(spent.ru_utime + spent.ru_stime) + int(limits["cpu_seconds"])
    resource.setrlimit(resource.RLIMIT_CPU, (budget, budget + 1))
    _set(resource.RLIMIT_NPROC, _task_count() + int(limits["process_headroom"]))


def _task_count() -> int:
    """How many tasks this process already has, for the NPROC headroom.

    `RLIMIT_NPROC` counts threads per uid, so the bound is relative. Returns one without `/proc`,
    which makes the limit stricter rather than absent.
    """
    try:
        return len(os.listdir("/proc/self/task"))
    except OSError:  # pragma: no cover — /proc is present on every supported platform.
        return 1


# Take the network away.


def _neutralise_network() -> None:
    """Make the socket module unusable for outbound traffic, references included.

    Patched on the module object so references libraries already hold are covered too. Outbound is
    `connect`/`connect_ex`, datagram `sendto`/`sendmsg`, and the `getaddrinfo`/`gethostbyname`
    lookups; serving (`bind`/`listen`/`accept`) is untouched. This duplicates
    `mcp_server_kit.egress` by hand because the isolated child cannot import it; it is a second
    layer behind the NetworkPolicy's `egress: []`.
    """
    import socket

    def _refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("the analysis sandbox has no network")

    socket.socket.connect = _refuse  # type: ignore[method-assign]
    socket.socket.connect_ex = _refuse  # type: ignore[method-assign]
    socket.socket.sendto = _refuse  # type: ignore[method-assign]
    socket.socket.sendmsg = _refuse  # type: ignore[method-assign]
    socket.create_connection = _refuse
    socket.getaddrinfo = _refuse
    socket.gethostbyname = _refuse
    socket.gethostbyname_ex = _refuse


def _restricted_builtins(jail: Path, opened: list[Any]) -> dict[str, Any]:
    """The builtins a program sees: all but `WITHHELD_BUILTINS`, plus guarded `__import__` and `open`.

    `jail` and `opened` are passed in so the jail is fixed before `exec()` runs.
    """
    namespace = {
        name: value for name, value in vars(builtins).items() if name not in WITHHELD_BUILTINS
    }
    namespace["__import__"] = _guarded_import
    namespace["open"] = _make_guarded_open(jail, opened)
    return namespace


# Run it, and encode what came back.


#: The envelope a raw `bytes`/`bytearray` value is wrapped in to cross the JSON boundary; the
#: : caller decodes it with `base64.b64decode(value["__b64__"])`, as `tools.py` documents.
BYTES_ENVELOPE_KEY = "__b64__"


def _encode(value: Any, limits: dict[str, Any], _stack: set[int] | None = None) -> Any:
    """Turn whatever a program assigned to `result` into something JSON can carry.

    JSON, never pickle: unpickling in the parent would be code execution outside the boundary.
    Anything a data format cannot describe becomes its `repr`. Frames and arrays are truncated by
    row so the output stays valid data. Recurses into containers so nested `bytes` get the envelope;
    `_stack` holds the ids on the current recursion path so a self-referencing container becomes a
    `repr` instead of crashing the runner on the recursion limit.
    """
    if _stack is None:
        _stack = set()
    numpy = sys.modules.get("numpy")
    pandas = sys.modules.get("pandas")
    rows = int(limits["result_rows"])

    if pandas is not None:
        if isinstance(value, pandas.DataFrame):
            body = json.loads(value.head(rows).to_json(orient="records", date_format="iso"))
            return {"columns": [str(c) for c in value.columns], "rows": body, "n_rows": len(value)}
        if isinstance(value, pandas.Series):
            head = json.loads(value.head(rows).to_json(date_format="iso"))
            return {"values": head, "n_rows": len(value)}
    if numpy is not None:
        if isinstance(value, numpy.ndarray):
            listed = value.tolist()
            return listed[:rows] if value.ndim == 1 else listed
        if isinstance(value, numpy.generic):
            return value.item()
    if isinstance(value, (bytes, bytearray)):
        return {BYTES_ENVELOPE_KEY: base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, (dict, list, tuple)):
        marker = id(value)
        if marker in _stack:
            raise ValueError("circular reference in result")
        _stack.add(marker)
        try:
            if isinstance(value, dict):
                return {str(key): _encode(item, limits, _stack) for key, item in value.items()}
            return [_encode(item, limits, _stack) for item in value]
        finally:
            _stack.discard(marker)
    if isinstance(value, (set, frozenset)):
        return sorted(str(item) for item in value)
    if isinstance(value, ModuleType):
        return f"<module {value.__name__}>"
    return value


def _dumps(value: Any, limits: dict[str, Any]) -> tuple[str | None, bool]:
    """JSON-encode the result and report whether the cap truncated it."""
    if value is None:
        return None, False
    try:
        text = json.dumps(_encode(value, limits), default=repr)
    except (TypeError, ValueError, RecursionError):
        # Backstop for a very deep (not cyclic) structure; cycles are caught in `_encode`.
        text = json.dumps(repr(value))
    cap = int(limits["result_chars"])
    return (text[:cap], True) if len(text) > cap else (text, False)


def _truncate(text: str, cap: int) -> tuple[str, bool]:
    """Cap captured output, reporting whether anything was dropped."""
    return (text[:cap], True) if len(text) > cap else (text, False)


def _caller_traceback(failure: BaseException) -> str:
    """The traceback, with this runner's own frames removed.

    Clearer for the caller, and it keeps the server's absolute source paths out of a tool result. A
    frame is the caller's when compiled under `ANALYSIS_FILENAME`; with none (a `SyntaxError`), the
    exception line alone is returned.
    """
    frames = [
        frame
        for frame in traceback.extract_tb(failure.__traceback__)
        if frame.filename == ANALYSIS_FILENAME
    ]
    lines = (
        ["Traceback (most recent call last):\n", *traceback.format_list(frames)] if frames else []
    )
    lines.extend(traceback.format_exception_only(type(failure), failure))
    return "".join(lines)


def main(argv: list[str]) -> int:
    """Read the payload, run the program, write the result.

    Non-zero means the runner failed (an internal error); a program that raised is a successful run
    with `error` populated.
    """
    with open(argv[1], encoding="utf-8") as source:
        payload = json.loads(source.read())
    limits: dict[str, Any] = payload["limits"]

    _apply_static_limits(limits)
    _neutralise_network()
    _apply_runtime_limits(limits)

    # Pinned before `exec()` and never re-read; see `_make_guarded_open`.
    jail = Path.cwd().resolve()
    opened_files: list[Any] = []

    namespace: dict[str, Any] = {
        "__name__": "__analysis__",
        "__builtins__": _restricted_builtins(jail, opened_files),
        "data": payload.get("data") or {},
        "result": None,
    }

    out, err = io.StringIO(), io.StringIO()
    error: str | None = None
    try:
        compiled = compile(payload["code"], ANALYSIS_FILENAME, "exec")
        with redirect_stdout(out), redirect_stderr(err):
            exec(compiled, namespace)  # noqa: S102 - running it is the whole capability
    # BLE001: a program's own failure is this tool's *result*, `SystemExit` included, so the catch
    # is deliberately wider than `Exception` rather than narrower.
    except BaseException as failure:  # noqa: BLE001
        error = _caller_traceback(failure)
    finally:
        # Reclaim leaked descriptors before the runner spends `RLIMIT_NOFILE` writing the result.
        for handle in opened_files:
            with contextlib.suppress(OSError):
                handle.close()

    stdout, out_cut = _truncate(out.getvalue() + err.getvalue(), int(limits["stdout_chars"]))
    encoded, result_cut = _dumps(namespace.get("result"), limits)

    with open(argv[2], "w", encoding="utf-8") as sink:
        sink.write(
            json.dumps(
                {
                    "stdout": stdout,
                    "result_json": encoded,
                    "error": error,
                    "truncated": out_cut or result_cut,
                }
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
