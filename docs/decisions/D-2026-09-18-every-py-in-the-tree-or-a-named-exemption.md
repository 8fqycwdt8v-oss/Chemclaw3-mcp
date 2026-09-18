# D-2026-09-18-every-py-in-the-tree-or-a-named-exemption — Every `.py` in the tree, or a named exemption

**Status:** accepted · **Date:** 2026-09-18 · **Commit:** the type gate's coverage ratchet derives
its basis from the tree instead of globbing four patterns and naming three roots. Corrects, without
superseding, the framing `D-2026-09-18-a-ratchet-that-observes-half-a-command-holds-half-a-gate`
shipped.

## The defect

That record widened `make type` to reach the root `conftest.py` and `scripts/`, and framed the sweep
that found them like this:

> `SRC` and `TESTS` are lists of **directories**, and two things in this tree are not directories …
> `scripts/`, which **is** a directory but was on neither variable.

The criterion contradicts itself inside three lines. `conftest.py` is not a directory; `tests` and
`scripts` are. What the three actually have in common is that **nothing globs them** — and the
ratchet was written to the stated criterion rather than the real one, as
`expected = {"tests", "conftest.py", "scripts"}` plus four glob patterns, under a comment reading
"Three of these are not directories under a glob, so they are named."

A third item fell through exactly that gap. Driven at `c6d2b3c`:

```
$ find . -name '*.py' -not -path './.venv/*' -not -path './.git/*' | wc -l
310
$ make type
Success: no issues found in 309 source files
$ (files under no root on the printed command)
servers/rxnpredict/scripts/fetch_models.py
```

The accounting is exact: one file, and it is the one script in this fleet whose own module docstring
says it "is *meant* to reach a network". `servers/*/scripts` is globbed by nothing, so the ratchet
was blind in **both** directions — it did not require that directory on the command, and a second
one appearing would not have been noticed either. A hand list cannot see what nobody thought to
write down, which is the same sentence as the record being corrected, one criterion further in.

## The decision

**The basis is the filesystem on both sides.** `test_the_type_gate_reads_the_test_tree_and_not_only_the_source`
now lists every `.py` this repository ships and requires each one to sit under some root on the
command `make -n type` prints. No pattern to keep in step, no literal to remember: a new
`packages/*/src`, a new `servers/*/tests`, a new `servers/*/scripts` and a directory nobody has
thought of yet are all covered the day a `.py` lands in one. Driven — a new `servers/props/bin/`
holding one trivially-correct module reds it, with no glob in the test mentioning `bin`.

The file list is `git ls-files --cached --others --exclude-standard -- '*.py'` rather than a
filesystem walk with directories to prune. The criterion wanted is "what this repository ships";
`.gitignore` already states it for `.venv`, the three caches and every build artefact, and a prune
list written into the test would be a second declaration of that — the hand list this change exists
to stop using, one layer down. `--others` includes a file somebody has just written and not staged,
which is when the gate most needs to notice it.

**`fetch_models.py` is gated rather than exempted.** `SRC` gains `$(wildcard servers/*/scripts)`,
globbed for the same reason `TESTS` already globs. It is not a clean add: the script imports
`huggingface_hub`, which is not installed in this workspace, so `mypy --strict` reported
`Cannot find implementation or library stub for module named "huggingface_hub" [import-not-found]`
and nothing else. That is answered with a per-module `ignore_missing_imports` beside the predictor
dependencies that already carry one — the one override shape
`D-2026-09-18-a-gate-that-does-not-read-the-tests-does-not-read-the-ratchets` argues is *not* a
narrowing, because it states a fact about a distribution rather than declining a check.

A `# type: ignore[import-not-found]` at the import site was the alternative and is worse in a way
worth recording: `huggingface_hub` ships `py.typed`, and the image this script runs in installs
`[reaction_t5]`, which brings it. So in the environment that actually executes the script the ignore
would be *unused*, and `warn_unused_ignores` would red on it. The override covers the absent case
only and lets the present case be checked, which is the behaviour both environments want.

**The test keeps its name although the name is now narrower than what it holds.** Five merged
records cite `test_the_type_gate_reads_the_test_tree_and_not_only_the_source` in their
`## What keeps it true`, and a merged record is never edited. Driven: renaming it reds
`tests/test_decision_log.py::test_every_test_a_record_names_still_exists` by name, which is that
ratchet doing its job. A citation that stops resolving is a retired check nobody notices; a name that
under-describes its function is a docstring's problem, and the docstring says so.

## What this does not claim

**Nothing was wrong in `fetch_models.py`.** Gated, it reports one error, and that error is a
distribution this workspace does not install. No defect is uncovered and none is claimed — what
changed is that a *second* ungated script cannot arrive in silence.

**It says nothing about what reading a root does.** `--exclude "_canary"` on the recipe passes this
test, because the path token is still on the command; it reds
`test_a_planted_error_in_a_gated_file_reds_make_type`
(`D-2026-09-18-a-ratchet-that-reads-the-right-artefact-and-never-checks-what-it-does`). The two are
complementary and neither subsumes the other.

**A deliberate exclusion is still possible and must be named.** The assertion message says so: a
file the gate should not read is a decision for a record and a named exemption in the test, not a
directory quietly absent from a variable.

## What keeps it true

- `tests/test_fleet.py::test_the_type_gate_reads_the_test_tree_and_not_only_the_source` — every
  `.py` `git ls-files` reports must sit under a root on the command `make -n type` prints. Driven
  red by: dropping `$(wildcard servers/*/scripts)` from `SRC` (`make type` falls 310 → 309),
  dropping the root `scripts`, dropping `conftest.py` from `TESTS`, dropping
  `$(wildcard servers/*/tests)`, dropping `$(SRC)` from the invocation, and by creating a
  `servers/props/bin/helper.py` that no pattern in the test mentions.
- `tests/test_decision_log.py::test_every_test_a_record_names_still_exists` — why the name above is
  kept; it reds on the rename, measured.
- `tests/test_fleet.py::test_the_type_gate_narrows_no_check_it_was_argued_out_of` — holds that the
  `ignore_missing_imports` override added here is the only shape permitted: it asserts no
  `disable_error_code` and no `warn_unused_ignores = false` in `[tool.mypy]` or any override.
