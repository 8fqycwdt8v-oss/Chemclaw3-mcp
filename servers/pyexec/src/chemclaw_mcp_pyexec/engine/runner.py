"""The child half of the sandbox: the only module in this repository that runs a caller's program.

It is executed as a **script**, by `sandbox.py`, as `python -I -B runner.py <payload> <result>`. It
imports nothing from its own package and must keep it that way: `-I` implies `-P`, so the script's
directory is not on `sys.path`, and a sibling import would fail in the child while passing every
test that imported this module directly.

## The order of operations is the design

Each step below is only correct where it is:

1. **Read the payload.** Before any limit, because a limit that stops us reading our own input turns
   a caller error into a runner crash.
2. **Apply the limits that cannot break an import** — address space, file size, descriptors. Doing
   these first means a program cannot escape them by failing early.
3. **Neutralise the network on the socket module itself.**
4. **Apply `RLIMIT_CPU` and `RLIMIT_NPROC`**, measured from here rather than from a constant:
   `RLIMIT_CPU` is cumulative over the process's whole life, and NPROC counts threads.
5. **Run the program** with a restricted builtins mapping and captured output.

**Nothing is pre-imported, and that was measured rather than assumed.** An earlier version warmed
numpy, pandas, four `scipy` submodules and RDKit before handing over, so that the caller's CPU
budget would not be spent on our imports. Measured in this child on this machine: an empty run is
**11 ms**, and that warm-up cost **1.2-1.9 s on every call** — `scipy.stats` alone is 1.6 s — which
an analysis needing only `math` paid in full. The lazy-import problem it was really protecting
against is solved by the guard below instead, so the warm-up bought latency and nothing else. A
program now pays for what it imports, and `cpu_seconds` is documented as including that.

## The import guard is a replaced `__import__`, and the first design was worse

The obvious construction — purge dangerous modules from `sys.modules` and refuse them again from a
`sys.meta_path` finder — was built, measured and thrown away. It fails in both directions at once.
It **breaks the libraries**: `scipy.optimize` imports `sys` lazily at call time, so a guard that
refuses `sys` turns `brentq` into a `SandboxImportError` (measured, before this rewrite). And it
**does not hold**, because `import` consults `sys.modules` before any finder, so the moment a
library re-imports `os` the cache is repopulated and a caller's `import os` never reaches the guard
at all.

Replacing `__import__` in the analysis namespace's builtins separates the two exactly. A caller's
`import X` resolves `__import__` from the mapping *its* frame was given; a library's import resolves
it from the real `builtins` module, which is untouched. One dictionary entry, no cache to keep
consistent, and no lazy import inside numpy has to be predicted in advance.

## What this is, and what it is not

**The import guard is not the security boundary.** It is ergonomics and defence in depth: it makes
the sandbox's shape obvious to an honest caller and raises the cost for a dishonest one. It is
porous by construction — `numpy` is reachable and the attribute graph hanging off a live library is
large — and treating it as a wall would be the mistake this repository files rows about, where a
control exists in order to be pointed at.

**The boundary is the process and the deployment**, and it holds even granting a complete escape
from everything in this file: a separate process killed by process group on a wall clock, hard
resource limits a non-root process cannot raise back, an environment built from an allowlist so no
credential is in it, a working directory deleted when the call returns, a rootless container with a
read-only root filesystem, and a NetworkPolicy with an empty `egress:` list.
"""

from __future__ import annotations

import builtins
import io
import json
import resource
import sys
import traceback
from collections.abc import Mapping, Sequence
from contextlib import redirect_stderr, redirect_stdout
from types import ModuleType
from typing import Any

#: The filename a caller's program is compiled under, so a traceback names the sandbox rather than
#: `<string>` — which reads, to a chemist, as though something went wrong inside the tool.
ANALYSIS_FILENAME = "<analysis>"

#: Root module names an analysis may import. Everything else is refused by name.
#:
#: An allowlist rather than a denylist, because the two fail in opposite directions: a name missing
#: from a denylist is reachable, while a name missing from here is a caller asking for it in a pull
#: request. The scientific four are the reason this server exists; the standard-library entries are
#: the ones an analysis actually reaches for — arithmetic, containers, text and dates.
ALLOWED_IMPORTS: frozenset[str] = frozenset(
    {
        # The reason this server exists.
        "numpy",
        "pandas",
        "scipy",
        "rdkit",
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

#: Builtins removed from the namespace a program runs in.
#:
#: `open` is the important one, and it is what makes `data` in and `result` out the only channel:
#: with no file object there is no way to read the container's filesystem or to leave anything
#: behind. The rest are either interactive (`input`, `help`, `breakpoint`), fatal to the runner
#: (`exit`, `quit`), or a second front door to the compiler that would skip the guarded `__import__`
#: entirely (`eval`, `exec`, `compile`).
#:
#: `__build_class__` stays, or no `class` statement works. `__import__` is not withheld but
#: *replaced* — see the module docstring.
WITHHELD_BUILTINS: frozenset[str] = frozenset(
    {"open", "input", "help", "breakpoint", "exit", "quit", "eval", "exec", "compile"}
)


class SandboxImportError(ImportError):
    """A program asked for a module the sandbox does not offer.

    Its own class so the message a chemist sees names the sandbox rather than looking like a broken
    deployment — "no module named socket" would send them to an operator with nothing to fix.
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

    The signature is `builtins.__import__`'s, positionally, because the `import` statement calls it
    positionally and a mismatch would fail at the first import rather than at review.
    """
    root = name.split(".", 1)[0]
    if root not in ALLOWED_IMPORTS:
        raise SandboxImportError(
            f"{root!r} is not available in the analysis sandbox. Available: "
            f"{', '.join(sorted(ALLOWED_IMPORTS))}."
        )
    return builtins.__import__(name, globals, locals, fromlist, level)


# --------------------------------------------------------------------------------------------
# Steps 2 and 4 — the resource limits.
# --------------------------------------------------------------------------------------------


def _set(which: int, value: int) -> None:
    """Set one rlimit's soft *and* hard bound, never raising above what we were given.

    Setting the hard bound too is what makes the limit stick: a soft limit alone can be raised back
    to the hard one by the very program being limited, in three lines and with no privileges. The
    `min` keeps this working unprivileged, where the inherited hard limit may already be lower than
    what we asked for and raising it would fail outright.
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
    """CPU and process count, applied last because both are measured from *now*.

    `RLIMIT_CPU` is cumulative over the process's whole life, so the caller's budget is added to
    what this runner has already spent starting up. The soft bound raises `SIGXCPU`; the hard
    bound a second later is `SIGKILL`, which is the backstop for a handler that ignores the first.
    """
    spent = resource.getrusage(resource.RUSAGE_SELF)
    budget = int(spent.ru_utime + spent.ru_stime) + int(limits["cpu_seconds"])
    resource.setrlimit(resource.RLIMIT_CPU, (budget, budget + 1))
    _set(resource.RLIMIT_NPROC, _task_count() + int(limits["process_headroom"]))


def _task_count() -> int:
    """How many tasks this process already has, for the NPROC headroom.

    `RLIMIT_NPROC` is counted per real user id and against tasks — threads included — so an absolute
    number would be either too low to let the BLAS keep the threads it already made, or high enough
    to be no bound at all. One on a kernel that does not expose `/proc`, which makes the limit
    stricter rather than absent.
    """
    import os

    try:
        return len(os.listdir("/proc/self/task"))
    except OSError:  # pragma: no cover — /proc is present on every supported platform.
        return 1


# --------------------------------------------------------------------------------------------
# Step 3 — take the network away.
# --------------------------------------------------------------------------------------------


def _neutralise_network() -> None:
    """Make the socket module unusable for outbound connections, references included.

    Replaced on the module object itself rather than hidden from `import`, because that is where
    every reference the warmed libraries already hold points. Hiding the name would leave those
    working and only inconvenience an honest caller.

    Serving is `bind`/`listen`/`accept` and is untouched, exactly as `mcp_server_kit.egress` reasons
    in the parent. This process serves nothing, but keeping the two ideas separate is what stops a
    later edit here breaking a server there by analogy.
    """
    import socket

    def _refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("the analysis sandbox has no network")

    socket.socket.connect = _refuse  # type: ignore[method-assign]
    socket.socket.connect_ex = _refuse  # type: ignore[method-assign]
    socket.create_connection = _refuse


def _restricted_builtins() -> dict[str, Any]:
    """The builtins a program sees: everything except `WITHHELD_BUILTINS`, plus a guarded import."""
    namespace = {
        name: value for name, value in vars(builtins).items() if name not in WITHHELD_BUILTINS
    }
    namespace["__import__"] = _guarded_import
    return namespace


# --------------------------------------------------------------------------------------------
# Step 5 — run it, and encode what came back.
# --------------------------------------------------------------------------------------------


def _encode(value: Any, limits: dict[str, Any]) -> Any:
    """Turn whatever a program assigned to `result` into something JSON can carry.

    JSON rather than pickle, and that is a security decision rather than a convenience one:
    unpickling is arbitrary code execution *in the parent*, which would hand back everything the
    child boundary exists to keep. So only values a data format can describe cross; anything else
    becomes its `repr`.

    Frames and arrays are truncated by row here rather than by character later, so what comes back
    is still valid data instead of a JSON document cut in half.
    """
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
    except (TypeError, ValueError):
        text = json.dumps(repr(value))
    cap = int(limits["result_chars"])
    return (text[:cap], True) if len(text) > cap else (text, False)


def _truncate(text: str, cap: int) -> tuple[str, bool]:
    """Cap captured output, reporting whether anything was dropped."""
    return (text[:cap], True) if len(text) > cap else (text, False)


def main(argv: list[str]) -> int:
    """Read the payload, run the program, write the result.

    A non-zero return means *the runner* failed, which the parent reports as an internal error. A
    program that raised is a successful run whose `error` field is populated — the distinction
    matters, because one is a defect here and the other is a Tuesday.
    """
    with open(argv[1], encoding="utf-8") as source:
        payload = json.loads(source.read())
    limits: dict[str, Any] = payload["limits"]

    _apply_static_limits(limits)
    _neutralise_network()
    _apply_runtime_limits(limits)

    namespace: dict[str, Any] = {
        "__name__": "__analysis__",
        "__builtins__": _restricted_builtins(),
        "data": payload.get("data") or {},
        "result": None,
    }

    out, err = io.StringIO(), io.StringIO()
    error: str | None = None
    try:
        compiled = compile(payload["code"], ANALYSIS_FILENAME, "exec")
        with redirect_stdout(out), redirect_stderr(err):
            exec(compiled, namespace)  # Running it is the whole capability.
    except BaseException:  # A program's own failure is data, `SystemExit` included.
        error = traceback.format_exc(limit=6)

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
