# D-2026-09-18-a-ratchet-that-observes-half-a-command-holds-half-a-gate — A ratchet that observes half a command holds half a gate

**Status:** accepted · **Date:** 2026-09-18 · **Commit:** the type-gate ratchet takes both halves of
`make -n type` off the wire instead of one. Corrects, without superseding, the control
`D-2026-09-18-a-gate-that-does-not-read-the-tests-does-not-read-the-ratchets` shipped.

## The defect

That record shipped two tests and described them as a pair: a source half
(`test_every_server_is_wired_into_the_type_gate`, unchanged) and a new test half
(`test_the_type_gate_reads_the_test_tree_and_not_only_the_source`) which took its basis off
`make -n type` *precisely because* — in its own docstring — "a basis that is re-derived rather than
observed will agree with itself forever."

That sentence is true of `TESTS` and **false of `SRC` in the adjacent function**, which reads the
`SRC :=` line out of the Makefile *text* and never looks at the recipe that is supposed to pass it.
So the pair observed one half of one command and re-derived the other. Driven at `6df6eb19`:

```
$ sed -i 's|--namespace-packages $(SRC) $(TESTS)|--namespace-packages $(TESTS)|' Makefile
$ git diff --numstat -- Makefile
1	1	Makefile
$ uv run pytest tests/test_fleet.py -k "type_gate or wired_into_the_type_gate" -q
2 passed, 198 deselected
$ make type
Success: no issues found in 141 source files
```

Against **305** on the unmutated tree. One character-class of edit takes **164 source files** —
every `src/` root in the workspace, the whole serving tree — out of `mypy --strict`, `make type`
reports `Success` louder than before, and **both** gate tests stay green.

That is the exact regression `test_every_server_is_wired_into_the_type_gate` was written to prevent.
Its own docstring recounts the shape twice: CI's hardcoded path list dropping `servers/safety/src`,
and then the Makefile's `SRC` silently dropping `servers/rxnlabel/src` and `servers/rxnpredict/src`
while `rxnlabel` sat with five real `--strict` errors CI had never run. Both of those were a
*declaration* going stale. This one is one level up and strictly worse: the declaration stays
correct and complete, and the recipe stops reading it.

## The decision

The observed command is the basis for **both** halves, and the source half is globbed rather than
listed, for the same reason `TESTS` already is:

```python
    for pattern in ("packages/*/tests", "servers/*/tests", "packages/*/src", "servers/*/src")
```

One line. Driven with the fix plus the same Makefile mutation: `1 failed, 1 passed`, naming all
twelve missing source roots.

**`test_every_server_is_wired_into_the_type_gate` is kept rather than folded in**, and the
difference is not redundancy. It holds the two *declarations* — the Makefile's `SRC` and
`pyproject.toml`'s `mypy_path` — against the servers on disk, which is a check about a text file
nobody invokes. This one holds the *invocation*. A server missing from `mypy_path` is invisible to
the glob (mypy would still read the files; it would resolve their imports differently), and a recipe
that has stopped passing `$(SRC)` is invisible to the declaration check. Neither sees the other's
failure, which is what makes them two tests.

## The same shape, one ratchet over: a substring is not a resolution

`test_the_build_group_names_every_backend_this_workspace_declares` shipped its lock half as
`f'name = "{name}"' not in lock` over `uv.lock`'s **text**, under a docstring claiming it catches
"a group entry `uv.lock` does not resolve". It does not. `name = "hatchling"` also appears under
`[package.dev-dependencies]` and `[package.metadata.requires-dev]`, which are *references* to a
resolution rather than the resolution. Driven:

```
$ sed -i '1485,1500d' uv.lock          # the whole [[package]] name = "hatchling" block
$ git diff --numstat -- uv.lock
0	16	uv.lock
$ .venv/bin/python -m pytest tests/test_fleet.py -k build_group -q
2 passed, 198 deselected
```

The severity is bounded and stated: `uv` itself refuses such a lock (`Failed to parse uv.lock`), so
a build fails loudly rather than silently — which is why this is a ratchet that does not hold what
it says rather than a hole in the pin. The fix is the idiom already present fifteen lines away in
`test_the_build_group_is_what_the_calc_image_exports`: parse the lock, read the resolved `name`s.
Driven with it, the same mutation reds with `uv.lock` resolves no `['hatchling']`.

The generalisation is the one this record is named for. A ratchet reads an *artefact*, and there
are two ways to get the artefact wrong: read the wrong half of it (the type gate, above) or read a
rendering of it instead of the thing (here). Both were written by sessions that had just argued
against exactly that.

## What this does not claim

Nothing was wrong on `6df6eb19` and nothing was uncovered: `make type` checked 305 files there and
checks 305 files here. What changed is what can go wrong *next* without a red line — which is the
whole argument the record being corrected makes about its own subject.

## What keeps it true

- `tests/test_fleet.py::test_the_type_gate_reads_the_test_tree_and_not_only_the_source` — requires
  every `packages/*/src`, `servers/*/src`, `packages/*/tests` and `servers/*/tests` directory on
  disk to appear in the command `make -n type` prints. A new server is covered in both halves the
  day its directories exist.
- `tests/test_fleet.py::test_every_server_is_wired_into_the_type_gate` — the declaration half,
  unchanged: `SRC :=` and `mypy_path` name every server that has a `src/`.
- `tests/test_fleet.py::test_the_build_group_names_every_backend_this_workspace_declares` — its
  lock half now parses `uv.lock` and compares resolved names, so a group entry the lock does not
  resolve is red whatever the file's text happens to contain elsewhere.
