# D-2026-09-18-a-gate-that-does-not-read-the-tests-does-not-read-the-ratchets — A gate that does not read the tests does not read the ratchets

**Status:** accepted · **Date:** 2026-09-18 · **Commit:** `make type` now reads the test tree under
full `--strict`, and the 153 errors that were waiting in it are fixed. Closes the `docs/BACKLOG.md`
§4 row that asked for the decision.

## The question the row asked

`$(SRC)` listed the twelve source roots and no test directory, so `mypy --strict` had never read the
files that drive every ratchet in this repository. The row recorded the hard half as already solved
— `MYPYPATH=. mypy --strict --explicit-package-bases --namespace-packages tests packages/*/tests
servers/*/tests` gets past the `Duplicate module named "test_no_egress"` collision that ten servers
now trigger — and asked the open half: **is a fix of every error worth the gate, or is a narrower
strictness for tests the right answer?** It warned against the obvious narrowing, since dropping
`--disallow-untyped-defs` alone removed 38 errors and none of the risk.

## What was measured

Re-measured at `0d58969`, three servers and ~27 commits after the row's figures:

```
$ MYPYPATH=. uv run mypy --strict --explicit-package-bases --namespace-packages \
    tests packages/*/tests servers/*/tests
Found 153 errors in 32 files (checked 141 source files)
```

Not 146 in 31. By code: 66 `attr-defined`, 38 `no-untyped-def`, 23 `arg-type`, 5 `operator`, 3
`import-untyped`, 3 `dict-item`, 2 each of `unused-ignore`, `union-attr`, `no-untyped-call`,
`index`, `call-arg`, `assignment`, and one each of `misc`, `comparison-overlap`, `call-overload`.

**The narrowing was measured before it was rejected, and the measurement is what rejects it.** Three
configurations, same tree, same commit:

| invocation | errors | files |
| --- | --- | --- |
| `--strict` | 153 | 32 |
| `--strict --disable-error-code=no-untyped-def` | 115 | 27 |
| the above, also without `attr-defined` and `arg-type` | 46 | 26 |

The third row is the trap. Of those 46, **22 are `unused-ignore`** against 2 under full strict —
because twenty `# type: ignore[attr-defined]` and `[arg-type]` comments in the tree become unused the
moment their code is disabled. So `--disable-error-code` and `warn_unused_ignores` fight: the
narrowing manufactures the one finding class that has a demonstrated yield here (the row's own
example is nine ignores in `servers/thermalsafety/tests` that suppressed nothing, already deleted).
A narrower strictness is therefore not a smaller version of this gate. It is a different gate whose
loudest signal is an artefact of its own configuration.

**What the fix actually cost is the other half of the answer, and it is not 153 edits.** The errors
were concentrated in a handful of loose annotations rather than spread over 32 files of test prose:

- `servers/chem/tests/test_sites.py` reported 24 of them. `_by_atom` was annotated
  `dict[int, object]` where it returns `dict[int, Site]`. One line, plus an import: **153 → 131**.
- 38 `no-untyped-def` were `def test_x():` with no `-> None`, applied mechanically from mypy's own
  note: **131 → 95**.
- 28 `attr-defined` were `no_implicit_reexport` on a module attribute a test patches —
  `readiness.mapping`, `selftest.distillation`, `safety_tools.screen_reaction`. Importing the module
  or the function directly is the *same object* and names where it comes from.

Two things are `# type: ignore` on purpose, and each is load-bearing rather than decorative:
`servers/unitops/tests/test_isolation_ops.py` omits a required argument **as its assertion**
(`call-arg` — if the parameter ever gained a default the ignore would go unused and the test would
go red with it), and `packages/mcp_server_kit/tests/test_sessions.py` installs `SimpleNamespace`
stand-ins in a mapping upstream types as transports.

**Nothing in `pyproject.toml` changed.** Two configuration relaxations were built and reverted, and
both reverts are the point. A `jsonschema.*` override contradicted an argued decision already in
`schema_cache.py` ("the ignores live here rather than as a blanket override in the root
`pyproject.toml`, where they would also hide a real error in somebody else's module"), so the test
files carry the same per-import ignore that module does. An `implicit_reexport = true` for `mcp.*`
made four ignores in `schema_cache.py` itself go unused — it was relaxing serving code in order to
quiet a test, which is the wrong direction, so the test carries four
`# type: ignore[attr-defined]` at the sites that read `lowlevel.jsonschema` instead.

## The decision

Full `--strict` over the test tree, in `make type`, with no check dropped and no configuration
relaxed. There is therefore **no residual risk statement to write**, which is the outcome the row's
second option could not have produced.

The recipe is one invocation rather than two: mypy builds one graph and the test tree imports the
source tree anyway. Measured cold at 0d58969, `make type` checks 305 source files.

**What this does not claim.** Twenty-two errors were read individually by the session that wrote the
row and about ten more by this one, and **none was a live defect** — the two candidates are a loop
variable rebound to a different type later in the same scope (`tests/test_consumer_agreement.py`,
now two names) and an `int | None` compared with `>` that would raise `TypeError` rather than fail
its assertion (`servers/chem/tests/test_species.py`). The gate is not justified by defects found. It
is justified by what it is *able* to see from now on: a ratchet mypy never reads is one that can
stop meaning what it says in silence, and the nine inert ignores this invocation found the first
time it was ever run are the shape.

## What keeps it true

- `tests/test_fleet.py::test_the_type_gate_reads_the_test_tree_and_not_only_the_source` — takes the
  command off `make -n type` rather than re-deriving it from the `TESTS` variable, and requires
  every `tests/`, `packages/*/tests` and `servers/*/tests` directory on disk to appear in it. A new
  server's tests are covered the day the directory exists. It also asserts
  `--explicit-package-bases`, without which the invocation does not run at all.
- `tests/test_fleet.py::test_every_server_is_wired_into_the_type_gate` — the source half, unchanged.
