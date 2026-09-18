# D-2026-09-18-a-ratchet-that-reads-the-right-artefact-and-never-checks-what-it-does — A ratchet that reads the right artefact and never checks what it does

**Status:** accepted · **Date:** 2026-09-18 · **Commit:** the type gate is bounded by running it
against a planted error rather than by parsing what it was configured with. Corrects, without
superseding, the guard `D-2026-09-18-a-ratchet-that-observes-half-a-command-holds-half-a-gate`
shipped.

## The defect

That record added `test_the_type_gate_narrows_no_check_it_was_argued_out_of` to hold the decision
"no check dropped and no configuration relaxed", and it names two ways to get a ratchet wrong: read
the wrong half of the artefact, or read a rendering of it instead of the thing. **There is a third,
and that guard ships it: read the right artefact correctly, and never check what it does.**

It parses `[tool.mypy]` off `pyproject.toml` — the right file, correctly — and then inspects exactly
two keys (`disable_error_code`, `warn_unused_ignores`) plus `strict`. It reads the command
`make -n type` prints — the right command, off the wire — and then matches four hand-written flag
names. mypy has dozens of ways to say "check less", and `make` has one character.

Driven at `c6d2b3c`, every run with this canary planted in a gated `src/` directory:

```python
# servers/props/src/chemclaw_mcp_props/_canary.py
def canary() -> int:
    return "not an int"
```

Unmutated it reds the gate: `Found 1 error in 1 file (checked 310 source files)` →
`make: *** [Makefile:56: type] Error 1`, exit 2. **Six one-line states leave it unreported with all
three gate tests green** (`uv run pytest tests/test_fleet.py -k "type_gate or
wired_into_the_type_gate" -q` → `3 passed, 203 deselected` in every row):

| state | numstat | `make type` says |
| --- | --- | --- |
| `ignore_errors = true` in `[tool.mypy]` | `1 0 pyproject.toml` | `Success: no issues found in 310 source files` |
| `[[tool.mypy.overrides]] module = ["chemclaw_mcp_props.*"]` + `ignore_errors = true` | `4 0 pyproject.toml` | `Success: no issues found in 310 source files` |
| a root `mypy.ini` with `ignore_errors = True` | a new file; **neither** `pyproject.toml` nor `Makefile` touched | `Success: no issues found in 310 source files` |
| `--exclude "_canary"` on the recipe | `1 1 Makefile` | `Success: no issues found in 309 source files` |
| a leading `-` on the recipe line | `1 1 Makefile` | prints the error, then `make: [Makefile:56: type] Error 1 (ignored)`; **`make type` exits 0** |
| `# mypy: ignore-errors` as line 1 of the canary | `1 0` | `Success: no issues found in 310 source files` |

Three of those are inside the guard's own field of view. `ignore_errors` is **one key** from the two
it inspects, and it turns off every check in all 310 files while `strict = true` stays literally
true — it also suppresses `unused-ignore`, so the `warn_unused_ignores` backstop
`D-2026-09-18-a-suppression-nobody-argued-reads-as-a-reviewed-one` leans on goes with it. `--exclude`
is squarely inside the stated job ("no relaxing flag on the command `make -n type` prints") and is
missed only because the list is hand-written. The `mypy.ini` row is the sharpest: it changes no
artefact either guard reads, so no amount of reading them better would have caught it.

The leading `-` matters beyond this repository's suite: `.github/workflows/ci.yml` runs `make type`
as its own step, so that state is green CI with the type error printed in the log.

## The decision

**Bound the gate by execution.** `tests/test_fleet.py::test_a_planted_error_in_a_gated_file_reds_make_type`
writes a module that violates four checks into one gated `src/` directory and one gated `tests/`
directory, runs **`make type`** — the real recipe, so the exit code is part of what is measured —
and requires a non-zero exit **and** each of the four error codes reported against each of the two
files. Every one of the six states above changes the *answer*, and the assertion is about the
answer, so all six red. Measured cost: one warm mypy run, 0.68 s.

Four violations rather than one, each chosen because a *different* relaxation silences it, driven
one at a time against the same two files:

| canary line | code | silenced by |
| --- | --- | --- |
| `def gate_canary_untyped():` | `no-untyped-def` | `--allow-untyped-defs`, `strict = false` |
| `return "not an int"` from `-> int` | `return-value` | `--disable-error-code=return-value` |
| `gate_canary_optional: int = None` | `assignment` | `--no-strict-optional` |
| `gate_canary_unused: int = 1  # type: ignore[assignment]` | `unused-ignore` | `--no-warn-unused-ignores`, `strict = false` |

A single-error canary would prove only that mypy still runs: `make type` stays red under every flag
the deleted deny-list used to name. Requiring *each code* is what makes the four names into four
measurements.

**The four-name flag deny-list is deleted, and `--disable-error-code` is kept.** The three names the
canary now covers by driving them are gone, because a name on a list and a code the gate is required
to report are not the same control — and only the second one noticed `--exclude`.
`--disable-error-code` stays on the recipe scan for a different reason: it is the narrowing
`D-2026-09-18-a-gate-that-does-not-read-the-tests-does-not-read-the-ratchets` measured and rejected
*by name*, and the recipe is the second place it can be written, which is the gap the previous
record found one level up. Re-taking a decision should fail against the sentence that rejected it.
The `[tool.mypy]` assertions stay for the same reason and with the same status, which the docstring
now states outright: **a named rejection, not a bound.**

## Why not the allow-list

The obvious alternative — invert both enumerations, allow-list the permitted `[tool.mypy]` keys and
the permitted recipe arguments — was considered and is worse on its own terms. It still never asks
mypy anything, so the `mypy.ini` row survives it untouched; it needs a second allow-list for the
override arm; and it turns every legitimate future configuration key into a test edit, which is the
"allowlist nobody reads" shape this repository declines elsewhere. Execution costs less and answers
the question actually being asked.

## What this does not claim

**It is a floor, not a proof of strictness.** A relaxation that silences some check no line of
`_TYPE_GATE_CANARY` violates passes. Raising the floor is adding a line to that constant, and the
four it carries are the four the deleted list named.

**It says nothing about which roots are gated.** That is
`test_the_type_gate_reads_the_test_tree_and_not_only_the_source`, which walks the tree; the canary
is planted at two named paths precisely so that deriving them from the command cannot make the check
agree with itself.

**Three tests, three different things, and one of them overlaps.** The declarations
(`test_every_server_is_wired_into_the_type_gate`), the invocation
(`test_the_type_gate_reads_the_test_tree_and_not_only_the_source`) and now the effect are not
substitutes. But the honest form of that: the `SRC :=` half of the declaration test **is** subsumed
by the invocation test — a root missing from `SRC :=` is a root missing from the printed command —
and it is kept for its message, which names the file and the line to edit. Its `mypy_path` half is
not subsumed by anything; nothing else reads that key.

**Nothing was wrong on `c6d2b3c`.** `pyproject.toml` is clean, no `mypy.ini` exists, the recipe
carries no relaxing flag, and `make type` really does red on a planted error. What changed is what
can go wrong next without a red line.

## What keeps it true

- `tests/test_fleet.py::test_a_planted_error_in_a_gated_file_reds_make_type` — plants a module
  violating four checks into a gated `src/` directory and a gated `tests/` directory, runs
  `make type`, and requires a non-zero exit and each code against each file. All six states in the
  table above red it; so do `--no-strict-optional`, `--allow-untyped-defs` and
  `--no-warn-unused-ignores` on the recipe, a `[tool.mypy] exclude` naming the canary, and a root
  `.mypy.ini`, each driven.
- `tests/test_fleet.py::test_the_type_gate_narrows_no_check_it_was_argued_out_of` — the named
  rejection that remains: `strict = true`, no `disable_error_code` and no `warn_unused_ignores =
  false` in `[tool.mypy]` or any override, and no `--disable-error-code` on the recipe. Reds on
  `--disable-error-code=arg-type`, which is a code the canary does not violate.
- `tests/test_fleet.py::test_the_type_gate_reads_the_test_tree_and_not_only_the_source` — unchanged
  in purpose: which roots the command reads.
