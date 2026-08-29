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

**The guarded `open` is the same kind of control, for the same reason.** It makes "you can write
and read files under your own scratch directory, nothing else" the honest shape of what `open()`
does, and it is exactly as porous as the import guard: a library the analysis reaches through an
allowed module can still write wherever *its own* unrestricted `open` reference points (see
`_guarded_open`).
What actually stops a write from leaving the pod is unrelated to `open` at all — the read-only root
filesystem, the rootless uid, and the fact that the scratch directory itself is destroyed on every
path out of the call. The jail is what makes an honest program's paths behave the way its author
expects; it is not why an escape cannot reach the filesystem.

**The boundary is the process and the deployment**, and it holds even granting a complete escape
from everything in this file: a separate process killed by process group on a wall clock, hard
resource limits a non-root process cannot raise back, an environment built from an allowlist so no
credential is in it *and* a parent that has made itself undumpable so its own environment cannot be
read back out of `/proc`, descriptors that lead nowhere the child can spend the server's memory
through, a per-call working directory deleted when the call returns, a rootless container with a
read-only root filesystem, and a NetworkPolicy with an empty `egress:` list.

Each of those is a claim about a mechanism, and two of them were false until an audit executed the
escape rather than reading about it: the allowlisted environment said nothing about the *parent's*,
and the output cap said nothing about fd 1. `README.md` records what the boundary covers and — the
half that matters more — what it does not.
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
        "matplotlib",
        "sympy",
        "sklearn",  # scikit-learn's import name; see pyproject.toml for the package name.
        "openbabel",
        # `from openbabel import pybel` imports root `openbabel` (the `from` name is not the root
        # `__import__` checks), so this line is not needed for that spelling. It is here anyway
        # because OpenBabel's own packaging has, on other platforms, shipped `pybel` as a second,
        # standalone top-level module — allowing the name costs nothing when it does not resolve
        # (`ModuleNotFoundError`, not a sandbox refusal) and avoids a surprise if it ever does here.
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

#: Builtins removed from the namespace a program runs in.
#:
#: `open` used to be here — see `_guarded_open` below for why it is a replacement instead of a
#: withholding, the same move `__import__` already made. The rest are either interactive (`input`,
#: `help`, `breakpoint`), fatal to the runner (`exit`, `quit`), or a second front door to the
#: compiler that would skip the guarded `__import__` entirely (`eval`, `exec`, `compile`).
#:
#: `__build_class__` stays, or no `class` statement works. `__import__` and `open` are not withheld
#: but *replaced* — see the module docstring and `_guarded_open`.
WITHHELD_BUILTINS: frozenset[str] = frozenset(
    {"input", "help", "breakpoint", "exit", "quit", "eval", "exec", "compile"}
)


class SandboxPathError(ValueError):
    """A program's `open()` call named a path outside its own scratch directory.

    Its own class, for the same reason as `SandboxImportError`: a chemist reading `ValueError:
    ... resolves outside the sandbox` can fix their program, where a bare `PermissionError` or
    `OSError` would read like the deployment, not the code, was broken.
    """


def _make_guarded_open(jail: Path, opened: list[Any]) -> Callable[..., Any]:
    """Build `open()` for one run: jailed to `jail`, tracking every handle it returns in `opened`.

    A **factory** rather than a free function, and both of the things it closes over fix a real
    bypass measured against an earlier version of this file that computed the jail from `Path.cwd()`
    fresh on every call.

    **The jail must be pinned before the analysis runs, not re-derived while it runs.**
    `Path.cwd()` is process state, and this sandbox's import guard is porous by construction (see
    the module docstring): `os` hangs off the attribute graph of modules already on
    `ALLOWED_IMPORTS` (`uuid.os` resolves without any guard refusing it), so a program that never
    writes `import os` can still reach `os.chdir()` and relocate what "here" means to a
    `Path.cwd()`-based check. Measured: with the jail re-read per call,
    `import uuid; uuid.os.chdir("/"); open("etc/passwd").read()` returned the real file — no
    import-guard escape, just an allowed import's own attribute. Computing `jail` once, in
    `main()`, before `exec()` ever runs, and closing over that value here removes the dependency on
    mutable process state entirely: the containment check is against a value the analysis namespace
    holds no reference to and cannot move.

    **Every handle this returns is appended to `opened`, so `main()` can close all of them before
    it writes its own result.** `open()` existing at all means a program can now leak file
    descriptors — open many files and close none, which does not require malice, just an unclosed
    loop — and `RLIMIT_NOFILE` is shared with the runner's own bookkeeping. Measured: a program
    that opens 1,000 files without closing any of them exhausts the descriptor budget, and the
    *runner's own* subsequent `open(argv[2], "w")` — writing a `result` the program had already
    computed correctly — then fails with the same `OSError`, discarding a good answer and
    reporting the run as `"killed"`, the same bucket a genuine memory or CPU ceiling uses.
    `main()` closes every handle in `opened` once `exec()` is done, successfully or not,
    reclaiming the budget before it needs any of it.
    """

    def _guarded_open(
        file: Any,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> Any:
        """`open()` for the analysis namespace: resolve against the pinned jail, then the real
        `open`.

        **No `**kwargs`, and that is the control, not an oversight.** Real `open()` also accepts
        `opener=`, a callback invoked as `opener(name, flags)` that may return a file descriptor for
        *any* path — it is not required to honour `name` at all. Forwarding it here would let a
        program pass a harmless in-jail `file=` argument alongside an `opener` that hands back a
        descriptor for something else entirely, defeating the containment check below without
        needing to reach `os`. Measured: with `opener` forwarded, `open("safe.txt", opener=lambda
        *_: os.open("/etc/passwd", os.O_RDONLY))` returned `/etc/passwd`'s contents while the
        checked path stayed `"safe.txt"`. There is no legitimate reason an analysis needs a custom
        opener, so the fix is not a check on its value — it is that this signature has nowhere to
        put it. `closefd` is dropped the same way; it only means something when `file` is already a
        descriptor, which is refused below regardless.

        A relative path resolves against `jail` exactly as an unguarded `open` would resolve against
        cwd; an absolute one is checked against it instead of trusted. Resolving through
        `Path.resolve()` — which follows `..` segments *and* symlinks — before the containment check
        is what catches both an absolute escape (`/etc/passwd`) and a relative one
        (`../../etc/passwd`), and a symlink planted inside the jail pointing out of it.

        **This is the only filesystem primitive the analysis namespace gets, and that is
        deliberate.** `os` and `pathlib` are not on `ALLOWED_IMPORTS` and must not be added for
        this: `pathlib.Path`'s own methods (`.open()`, `.read_text()`, `.write_bytes()`, …) call
        `io.open` through *pathlib's own* module globals, not through the analysis namespace's
        restricted `__builtins__` — the exact bypass this module's docstring already describes for
        `import` (a library's own reference to a name resolves from the real `builtins`/
        `sys.modules`, untouched, because only the caller's own frame was given the restricted
        mapping). Exposing `pathlib` would hand back a second, unguarded filesystem door labelled as
        though it were the guarded one. The same reasoning is why `os` stays out: `os.open`/
        `os.fdopen` are a second door with no jail on them at all.

        A file descriptor (`open(3)`) is refused for the same reason a bypass would be: an int names
        whatever fd the child process already has open, which nothing here can jail after the fact —
        an escaped `os` reference could hand the analysis a descriptor for something outside the
        scratch directory, so the type is refused up front rather than resolved.

        **What this does not, and cannot, cover.** A library that receives a path from the
        analysis and opens it *through its own, unrestricted `open` reference* —
        `rdkit.Chem.MolToMolFile(mol, path)`, `matplotlib.pyplot.savefig(path)` — does not go
        through this function at all, for the identical reason `pathlib` cannot be made safe by
        adding it to the allowlist: the library's internal call resolves the real
        `builtins.open`, not the analysis namespace's guarded one. That write lands wherever the
        path argument points, jail or not. See `README.md`'s residual-risks paragraph.
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
        # A context manager isn't an option: the handle must outlive this call and be returned to
        # the analysis, which is exactly why `opened` exists — `main()` closes it once `exec()`
        # is done rather than leaving it to the caller's own (possibly absent) `close()`.
        handle = builtins.open(resolved, mode, buffering, encoding, errors, newline)  # noqa: SIM115
        opened.append(handle)
        return handle

    # Named `open`, not `_guarded_open`: Python's own argument-binding `TypeError` (e.g. for a
    # rejected `opener=`) quotes a callable's `__qualname__`, and a chemist seeing "open() got an
    # unexpected keyword argument" can act on it, where "_make_guarded_open.<locals>._guarded_open()
    # got an unexpected keyword argument" reads like the deployment is broken rather than the
    # program — the same "name the sandbox, not the internals" reasoning `SandboxPathError` and
    # `SandboxImportError` are already for.
    _guarded_open.__name__ = "open"
    _guarded_open.__qualname__ = "open"
    return _guarded_open


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

    **`connect`/`connect_ex` are not the whole of "outbound", and this used to patch only those.**
    `mcp_server_kit.egress` measured the same gap in the parent process and its docstring says why:
    a datagram socket never calls `connect` either, so `sendto`/`sendmsg` carried a payload straight
    out, and `getaddrinfo`/`gethostbyname` are module-level C functions a DNS-based exfiltration or
    licence check reaches with no `connect` call at all. This process cannot import that module (it
    runs as an isolated script with no sibling-package imports, by design — see the module
    docstring), so the same five names are patched here, by hand, to stay a real second layer behind
    the deployment's `egress: []` NetworkPolicy rather than a control that reads as one and is not.
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
    """The builtins a program sees: everything except `WITHHELD_BUILTINS`, plus the two guarded
    replacements — `__import__` and `open` — that stand in for what would otherwise be withheld
    outright.

    Takes `jail` and `opened` rather than deriving them itself, so the value `open()` is jailed to
    is fixed at the moment this namespace is built — before `exec()` runs — and cannot be re-read
    from process state the analysis could already have changed. See `_make_guarded_open`.
    """
    namespace = {
        name: value for name, value in vars(builtins).items() if name not in WITHHELD_BUILTINS
    }
    namespace["__import__"] = _guarded_import
    namespace["open"] = _make_guarded_open(jail, opened)
    return namespace


# --------------------------------------------------------------------------------------------
# Step 5 — run it, and encode what came back.
# --------------------------------------------------------------------------------------------


#: The envelope a raw `bytes`/`bytearray` value is wrapped in to cross the JSON boundary.
#:
#: One key, chosen to be both unambiguous (nothing a program's own `dict` would plausibly use) and
#: documented to the model in `tools.py`'s docstring, since decoding it is the caller's job on the
#: other side: `base64.b64decode(value["__b64__"])`.
BYTES_ENVELOPE_KEY = "__b64__"


def _encode(value: Any, limits: dict[str, Any], _stack: set[int] | None = None) -> Any:
    """Turn whatever a program assigned to `result` into something JSON can carry.

    JSON rather than pickle, and that is a security decision rather than a convenience one:
    unpickling is arbitrary code execution *in the parent*, which would hand back everything the
    child boundary exists to keep. So only values a data format can describe cross; anything else
    becomes its `repr`.

    Frames and arrays are truncated by row here rather than by character later, so what comes back
    is still valid data instead of a JSON document cut in half.

    Recurses into `dict`/`list`/`tuple`, because the realistic shape for a binary result is nested
    — `{"plot_png": open("plot.png", "rb").read()}` — not a bare `bytes` assigned straight to
    `result`. `json.dumps`'s own traversal would otherwise reach an un-encoded `bytes` value and
    fall through to `default=repr`, producing a Python `repr` string rather than the documented
    envelope.

    `_stack` tracks which containers are on the *current recursion path* (by `id()`), added on
    entry and discarded on exit — the same scheme `json.dumps`'s own C encoder uses internally,
    and for the same reason: a value referenced twice from different branches of an otherwise
    ordinary structure is fine and must not be flagged, but a container that reaches itself is not
    something this function can render, ever, so recursing further into it does not help. Before
    recursion was added here, `json.dumps` caught this itself and raised `ValueError`, which
    `_dumps` below already turns into a `repr()` fallback — an easy accident
    (`result = []; result.append(result)`, e.g. while assembling a graph or tree) degraded
    gracefully. Without an equivalent check here, `_encode`'s own recursion walked the cycle with
    plain Python calls until it hit Python's recursion limit and raised `RecursionError`,
    uncaught, crashing the whole runner before it could write a result — for the same caller
    mistake that used to be an ordinary, reported error.
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
        # `RecursionError` is a backstop for a merely very deep (not cyclic) structure — the cycle
        # itself is caught above, inside `_encode`, well before Python's own recursion limit fires.
        text = json.dumps(repr(value))
    cap = int(limits["result_chars"])
    return (text[:cap], True) if len(text) > cap else (text, False)


def _truncate(text: str, cap: int) -> tuple[str, bool]:
    """Cap captured output, reporting whether anything was dropped."""
    return (text[:cap], True) if len(text) > cap else (text, False)


def _caller_traceback(failure: BaseException) -> str:
    """The traceback, with this runner's own frames removed.

    Two reasons, and the second is the one that made this a change rather than a preference. It is
    **clearer**: the caller wants the line in their own program, and `runner.py`'s `exec` frame is
    noise they cannot act on. And it is **narrower**: an unfiltered traceback prints this server's
    absolute source paths into a tool result that a model reads and may quote into an answer —
    telling a caller where the sandbox lives is not a capability anybody asked for.

    A frame is the caller's when it was compiled under `ANALYSIS_FILENAME`, which nothing else in
    this process is. When none are — a `SyntaxError` fails before any frame exists — the exception
    line alone is returned, which is exactly what a caller needs to fix it.
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

    # Pinned here, before `exec()` ever runs, and never re-read afterwards — see
    # `_make_guarded_open`'s docstring for the escape this closes (an analysis relocating `cwd`
    # and taking a `Path.cwd()`-based jail with it).
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
            exec(compiled, namespace)  # Running it is the whole capability.
    except BaseException as failure:  # A program's own failure is data, `SystemExit` included.
        error = _caller_traceback(failure)
    finally:
        # Reclaim every descriptor the analysis opened and never closed, before this runner spends
        # any of its own `RLIMIT_NOFILE` budget writing the result — see `_make_guarded_open`.
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
