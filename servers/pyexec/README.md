# `pyexec` — run a short Python analysis, offline and bounded

One tool, `run_python`. The agent sends a program and a JSON payload; the program runs in a
disposable child process with **numpy, pandas, scipy and RDKit** importable, and whatever it assigns
to `result` comes back.

It exists because the arithmetic between tool calls had nowhere to happen. Fitting a curve to points
another tool returned, aggregating a table, converting units, canonicalising a SMILES, checking a
mass balance — none of that is worth its own server, and all of it was previously impossible.

```
run_python(code: str, data: dict | None = None) -> RunResult
```

| Port | Token | Classification |
| --- | --- | --- |
| 8899 | `CHEMCLAW_PYEXEC_TOKEN` | `read_only` |

`read_only` is a deliberate reading, not a technicality: the tool writes nothing, persists nothing,
and has no effect outside a directory deleted before it returns. What the classification decides is
whether the agent may use it *while building the plan a human is asked to approve* — and "which of
these two routes has the lower E-factor" has to be answerable before somebody signs off, not after.

## What it is not

- **Not a shell.** There is no `subprocess`, no `os`, no command line.
- **Not a notebook.** Nothing survives a call. A name bound in one run does not exist in the next,
  so the whole analysis goes in one program.
- **Not a file tool.** `open` is not in builtins. `data` in and `result` out are the only channels.
- **Not a data source.** It computes over what the caller gives it and knows nothing itself. A
  number it returns is only as good as the numbers in `data` — cite those, not this.

## The controls, and which of them are the boundary

**Read this table before changing anything in `engine/` or `deploy/`.** The distinction between the
two halves is the whole security design, and a change that quietly moves a row from the second half
to the first is the change that matters.

### Defence in depth — real, and porous by construction

| Control | Where |
| --- | --- |
| A guarded `__import__` in the analysis namespace, allowlisting ~30 modules | `engine/runner.py` |
| `open`, `eval`, `exec`, `compile`, `input`, `breakpoint`, `exit`, `quit` withheld from builtins | `engine/runner.py` |
| `socket.connect`, `connect_ex` and `create_connection` replaced with a refusal | `engine/runner.py` |

These make the sandbox's shape obvious to an honest caller and raise the cost for a dishonest one.
**They are not a wall.** `numpy` is reachable and the attribute graph hanging off a live library is
large; a Python-level sandbox is a research area rather than a solved problem. Treating this half as
the boundary would be a control that exists in order to be pointed at.

### The boundary — holds even granting a complete escape from the half above

| Control | Where |
| --- | --- |
| A separate, disposable process — never `exec` in the server | `engine/sandbox.py` |
| Killed by **process group** on the wall clock, so children die with it | `engine/sandbox.py` |
| `RLIMIT_CPU`, `AS`, `FSIZE`, `NOFILE`, `NPROC` — **soft and hard**, so they cannot be raised back | `engine/runner.py` |
| An environment **built from an allowlist**, so no credential is in it | `engine/sandbox.py` |
| `python -I -B`, a fresh temp working directory deleted on return, `HOME` inside it | `engine/sandbox.py` |
| Rootless image, and the pod should mount the root filesystem read-only | `Containerfile` |
| `NetworkPolicy` with an empty `egress:` — not even DNS | `deploy/networkpolicy.yaml` |

The environment is **built rather than filtered** on purpose: deleting known-dangerous names from a
copy of `os.environ` works until somebody adds a variable nobody thought of, and this pod's
environment carries a bearer token for this very server.

## Two design decisions worth knowing before you edit

**The import guard is a replaced `__import__`, and the obvious design was worse.** Purging modules
from `sys.modules` and refusing them from a `sys.meta_path` finder was built, measured and thrown
away. It broke the libraries — `scipy.optimize` imports `sys` lazily at call time, so `brentq`
raised `SandboxImportError` — *and* it did not hold, because `import` consults `sys.modules` before
any finder, so the first library to re-import `os` repopulated the cache and a caller's `import os`
never reached the guard. Replacing `__import__` in the analysis namespace separates caller from
library exactly: one dictionary entry, no cache to keep consistent.

**Nothing is pre-imported.** An earlier version warmed numpy, pandas, four scipy submodules and
RDKit so the caller's CPU budget would not be spent on our imports. Measured: an empty run is
**11 ms** and that warm-up cost **1.2–1.9 s on every call** (`scipy.stats` alone is 1.6 s), paid in
full by an analysis needing only `math`. The lazy-import problem it protected against is solved by
the guard instead, so it bought latency and nothing else. A program now pays for what it imports,
and `cpu_seconds` includes that.

## `runner.py` is exempt from the egress scan, and owes a test for it

`engine/runner.py` is the one file in this fleet that imports `socket`. It runs **in the child**, and
it imports the module in order to replace its outbound calls with a refusal — the opposite of
egress, and unreachable from the process the scan protects. `tests/test_no_egress.py` pays for the
exemption: it parses `runner.py` and asserts that every attribute it touches on the `socket` module
is an assignment, and that the three of them are exactly the outbound calls. An edit that *reads*
something off that module fails there.

The alternative — arming `mcp_server_kit`'s own guard in the child — was measured and rejected:
importing the kit costs **730 ms** against a 13 ms bare interpreter, on every analysis.

## Bounds

Defaults are in `engine/limits.py`; every one is a field with its reasoning beside it.

| | Default | |
| --- | --- | --- |
| Wall clock | 20 s | the bound that always holds |
| CPU | 15 s | includes the program's own imports |
| Memory | 2 GiB | `RLIMIT_AS`; the container limit is the real ceiling |
| Output | 10,000 chars | a cap on the caller's *context*, never silent |
| Result | 20,000 chars / 200 rows | frames are cut by row, so what returns still parses |

## Running it

```sh
make run-pyexec                 # 127.0.0.1:8899 with a dev token
uv run pytest servers/pyexec    # 45 tests, ~10 s
```
