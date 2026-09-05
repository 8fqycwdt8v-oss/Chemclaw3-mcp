# `pyexec` — run a short Python analysis, offline and bounded

One tool, `run_python`. The agent sends a program and a JSON payload; the program runs in a
disposable child process with **numpy, pandas, scipy, matplotlib, sympy, scikit-learn, RDKit and
OpenBabel** importable, and whatever it assigns to `result` comes back — files it writes and reads
back along the way stay confined to that one call's own scratch directory.

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
- **Not a notebook.** Nothing the tool offers survives a call. A name bound in one run does not
  exist in the next, so the whole analysis goes in one program.
- **Not persistent storage.** `open` is in builtins again (2026-08-29), but every path it resolves —
  relative or absolute — is jailed to that one call's own scratch directory, which is destroyed with
  the rest of the call. `data` in, `result` out, and a file for the *duration of one call* are the
  three channels; there is no way to leave something for a later call to find.
- **Not a data source.** It computes over what the caller gives it and knows nothing itself. A
  number it returns is only as good as the numbers in `data` — cite those, not this.

## The controls, and which of them are the boundary

**Read this table before changing anything in `engine/` or `deploy/`.** The distinction between the
two halves is the whole security design, and a change that quietly moves a row from the second half
to the first is the change that matters.

### Defence in depth — real, and porous by construction

| Control | Where |
| --- | --- |
| A guarded `__import__` in the analysis namespace, allowlisting ~35 modules | `engine/runner.py` |
| A guarded `open`, resolving every path against a jail pinned before the analysis runs | `engine/runner.py` |
| `eval`, `exec`, `compile`, `input`, `breakpoint`, `exit`, `quit` withheld from builtins | `engine/runner.py` |
| `socket.connect`, `connect_ex` and `create_connection` replaced with a refusal | `engine/runner.py` |

These make the sandbox's shape obvious to an honest caller and raise the cost for a dishonest one.
**They are not a wall.** `numpy` is reachable and the attribute graph hanging off a live library is
large; a Python-level sandbox is a research area rather than a solved problem. Treating this half as
the boundary would be a control that exists in order to be pointed at.

**The guarded `open` belongs in this half, not the next one, and that is worth being explicit
about.** It is ergonomics and defence in depth exactly like the import guard: it makes "files stay
under your own scratch directory" the honest shape of `open()`'s behaviour for an honest caller, and
it is exactly as porous — a library the analysis reaches through an *allowed* module (`rdkit`,
`matplotlib`) can still write wherever *its own*, unrestricted `open` reference points, because that
call never goes through the guard at all (see `_guarded_open`'s docstring). What actually keeps a
write from leaving the pod is the next table: the read-only root filesystem and the deployment, not
the jail.

### The boundary — holds even granting a complete escape from the half above

| Control | Where |
| --- | --- |
| A separate, disposable process — never `exec` in the server | `engine/sandbox.py` |
| Killed by **process group** on the wall clock — paired with a **zero fork headroom** so no child can `setsid` out of that group and survive | `engine/sandbox.py`, `engine/runner.py` |
| `RLIMIT_CPU`, `AS`, `FSIZE`, `NOFILE`, `NPROC` — **soft and hard**, so they cannot be raised back | `engine/runner.py` |
| An environment **built from an allowlist**, so no credential is in it | `engine/sandbox.py` |
| The parent made **undumpable before it forks**, so the child cannot read `/proc/<ppid>/environ` | `engine/sandbox.py` |
| The child's stdout **discarded** and its stderr written to a file under the child's own `RLIMIT_FSIZE` | `engine/sandbox.py` |
| `python -I -B`, a per-call temp directory that is `HOME`, `TMPDIR` and the working directory, removed on every path including the kill | `engine/sandbox.py` |
| Rootless image, and the pod should mount the root filesystem read-only | `Containerfile` |
| `NetworkPolicy` with an empty `egress:` — not even DNS | `deploy/networkpolicy.yaml` |

The environment is **built rather than filtered** on purpose: deleting known-dangerous names from a
copy of `os.environ` works until somebody adds a variable nobody thought of, and this pod's
environment carries a bearer token for this very server.

**A clean child environment was only half of that, and an escape proved it.** Parent and child run
as the same uid, so `/proc/<ppid>/environ` — mode 0400, owned by that uid — handed a program that
reached `os` this server's own bearer token, and it came back to the caller inside `result`, which
is exfiltration by the route the request arrived on and something an empty `egress:` cannot see. So
the parent sets `PR_SET_DUMPABLE=0` before it forks, which re-owns its `/proc` entry to root and
refuses the same-uid child with `EACCES`. A PID namespace would hide `/proc/<ppid>` outright and is
the stronger form, but it needs `CAP_SYS_ADMIN` or an unprivileged user namespace and a rootless
pod is promised neither. **Root is exempt from the rule** — `CAP_SYS_PTRACE` reads any `/proc` —
which is one more reason this image ships `USER 1001`.

**The output caps are on the caller's context, not on the server's memory**, and the two were
confused. `stdout_chars` truncates what the runner captures; a program that reached `os` and wrote
to fd 1 directly went past it, and every byte was read into *this* process and then discarded —
1.5 GB written took the parent's RSS to +2981 MiB, which is an OOM kill of every session the pod is
serving rather than of the one run. Nothing here ever wanted those bytes, so the child's stdout is
`DEVNULL` and its stderr goes to a file inside the scratch directory, where the child's own
`RLIMIT_FSIZE` bounds it and the parent reads a 4 KiB tail for the one line it needs.

**The group kill was not the whole of "children die with it", and an escape proved that too.**
`killpg` reaches one process group; a program that reached `os`, forked, and had the grandchild call
`setsid()` put it in a *new* session, and the kill of the original group left it running — an orphan
that outlived the wall clock by seconds, still holding the scratch directory. The fix is not a
bigger kill but a foreclosed fork: `Limits.process_headroom` is **0**, so `RLIMIT_NPROC` refuses the
child any new task and there is no grandchild to orphan (nor any fork bomb). Measured as a non-root
uid, which is how this runs: headroom `16` let the orphan survive, headroom `0` refused the `fork`
with `EAGAIN`. It costs a worker *thread* too, which is free here — the analysis is single-process,
every BLAS is pinned to one thread, and `threading`/`concurrent.futures` are not importable — and a
run over numpy/pandas/scipy/RDKit is unchanged by it. `RLIMIT_NPROC` is unenforced for root, so this
leans on `USER 1001` exactly as the other rlimits do. What it does **not** close — a same-uid signal
to the parent, and a private network stack — needs a PID/NET namespace, which a rootless pod is not
promised; those stay covered by the process boundary, the parent seal, the in-child `socket`
neutralisation and the `egress: []` policy.

**What the boundary does not cover, stated rather than implied.** The per-call scratch directory is
private to a run and is removed on every path, the kill included — but it is not a *confinement*.
The pod's `/tmp` is an emptyDir shared by every call for the pod's lifetime, and a program that has
escaped the import guard (reached a raw `os` reference) can write an absolute path into it and have
a later call read it back (measured; bounded by `RLIMIT_FSIZE` per file and by the pod's lifetime).
Nothing in this process can close that without a mount namespace, so it is written down here instead
of being promised away: what holds is that each call gets its own directory and leaves none behind,
and what bounds the rest is the deployment — a read-only root filesystem, the emptyDir's lifetime,
and the pod itself.

**The guarded `open` sits inside that same residual, not outside it, and this is the second half of
the two-decisions section below spelled out here.** A library the analysis passes a path to —
`rdkit.Chem.MolToMolFile(mol, path)`, `matplotlib.pyplot.savefig(path)` — writes through its *own*
unrestricted `open` reference, never through `_guarded_open`, because that call is made from the
library's own frame with the real, untouched `builtins`. So an absolute path handed to a library is
still the caller's own responsibility exactly as it was before `open` was restored: the guard governs
what the analysis's *own* `open(...)` calls can reach, not what a library does with a path it is
given. What bounds a library-internal write to the same degree as everything else is unchanged —
the read-only root filesystem and the rootless uid refuse a write anywhere but the scratch directory
and the documented shared-`/tmp` residual above.

**The jail was pinned to the wrong thing for a while, and two measured bypasses proved it — neither
one was an escape from anything, both were ordinary allowed code.** The first version of
`_guarded_open` computed `jail = Path.cwd().resolve()` fresh on every call, trusting the process's
current working directory as the containment boundary. `os` hangs off the attribute graph of modules
already on `ALLOWED_IMPORTS` (`uuid.os`, for one) — the import guard's own porousness, stated above —
so a program never importing `os` directly could still reach `os.chdir("/")` and relocate what "here"
meant to every `open()` call after it. Measured: `import uuid; uuid.os.chdir("/");
open("etc/passwd").read()` returned the real file. Separately, `_guarded_open` forwarded `*args` and
`**kwargs` straight to the real `open()` after checking only `file`, and real `open()` accepts an
`opener=` callback that receives `(name, flags)` and may return a descriptor for any path at all,
independent of `name`. Measured: `open("safe.txt", opener=lambda *_: os.open("/etc/passwd",
os.O_RDONLY))` returned `/etc/passwd`'s contents while the checked path stayed `"safe.txt"`. Fixed
the same day (2026-08-29): the jail is now computed once, in `main()`, before `exec()` ever runs, and
closed over by the `open()` a run's namespace gets, so nothing the analysis does afterward can move
it; and `_guarded_open`'s signature takes no `**kwargs` at all, so `opener` (and `closefd`, which only
ever meant anything for a descriptor `_guarded_open` already refuses) has nowhere to be passed.
Neither bypass reached the network, a credential, or a write outliving the call — both were bounded
to what the pod's own uid could already read or write, the same ceiling the residual paragraphs above
already accept — so the `read_only` classification never needed to change while they were open; they
are recorded here because until this fix, "the jail decides what an honest program's paths mean" was
true of the *intent* and not, for these two shapes of call, of the code.

## Three design decisions worth knowing before you edit

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

**`os` and `pathlib` stay off `ALLOWED_IMPORTS` even though `open` is back, and that is a decision
rather than an oversight.** `pathlib.Path`'s own methods (`.open()`, `.read_text()`,
`.write_bytes()`, …) call `io.open` through *pathlib's own* module globals, not through the analysis
namespace's restricted `__builtins__` — the identical bypass the import-guard section above already
describes: a library's own reference to a name resolves from the real, unrestricted `builtins` and
`sys.modules`, because only the caller's own frame was ever given the guarded mapping. Adding
`pathlib` to the allowlist would not extend the jail to it; it would hand back a second, unguarded
filesystem door dressed up as though it were the guarded one, and the same reasoning is why `os`
(`os.open`, `os.fdopen`, and every path-taking call in it) stays out too — a door with no jail on it
at all. `_guarded_open` in `engine/runner.py` is written up as the one filesystem primitive the
analysis namespace gets, for exactly this reason.

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
| One file | `RLIMIT_FSIZE`, 16 MiB | `engine/limits.py`'s `file_bytes`; unchanged by the jail |

**There is deliberately no separate cap on the scratch directory's *total* size.** `RLIMIT_FSIZE`
already bounds any single file a program writes — a plot, a scratch CSV, an intermediate mol2 — the
same pattern this server already uses for `RLIMIT_AS` against the container's own memory limit: a
coarse process-level bound, backstopped by a deployment-level one rather than a second in-process
counter. Total ephemeral usage across a pod's *concurrent* calls is that deployment's to bound —
today's `deploy/deployment.yaml` sets CPU and memory requests/limits but no `ephemeral-storage`
entry, which is a gap in the same family as those two rather than something this change closes; a
pod-level `ephemeral-storage` limit belongs beside the CPU/memory pair there whenever that gap is
picked up, not as a per-run field in `engine/limits.py` — `RLIMIT_FSIZE` already answers "how much
can one program write", and a per-run counter cannot answer "how much can this pod hold at once".

## `/healthz` forks

Every other server in the fleet is unready when a *file* will not load; this one is unready when a
*process* will not run. Nothing touches the sandbox at import, so the interpreter on the image,
`runner.py` beside it, the `prctl` seal the parent applies before forking and the limits the child
sets on itself were all first exercised by whichever caller arrived first — and until 2026-09 this
server passed no `readiness=` at all, so a pod with a broken interpreter or an unwritable scratch
directory answered a constant 200 and failed every call. `engine/readiness.py` runs one trivial
program through the same `sandbox.run` a tool call uses and checks what came back; a failure is a
503 naming the reason.

It is cached for the life of the process, deliberately. A fresh probe forks, and at a ten-second
probe interval that is both ~66 ms of CPU each time and one increment of
`chemclaw_mcp_pyexec_runs_total{outcome="ok"}` — about 8,600 a day of the server watching itself,
buried in the counter that exists to describe callers. What the probe checks is an image property.
`lru_cache` does not cache exceptions, so a pod that starts broken is re-probed and stays 503.

## Running it

```sh
make run-pyexec                 # 127.0.0.1:8899 with a dev token
uv run pytest servers/pyexec    # ~29 s
```
