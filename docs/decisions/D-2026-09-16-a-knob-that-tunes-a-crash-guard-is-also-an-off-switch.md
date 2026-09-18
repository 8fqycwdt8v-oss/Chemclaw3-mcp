# D-2026-09-16-a-knob-that-tunes-a-crash-guard-is-also-an-off-switch — A knob that tunes a crash guard is also an off switch

**Status:** accepted · **Date:** 2026-09-16 · **Commit:** a `maximum` on `env_bound`, passed at
the one site where exceeding it is a crash, and the third composition comparison in
`shortcut_column`. Supersedes nothing; it is the symmetric half of
`D-2026-09-15-five-copies-varied-the-message-not-the-mechanism`'s floor.

## Context

`mcp_server_kit.limits` exists for one reason, and its module docstring says so in the first
paragraph: RDKit's canonicaliser recurses over the molecular graph, a large enough molecule
overflows the C stack, and the process dies with SIGSEGV — which no `try`/`except` in Python can
catch. One authenticated tool call takes the pod down and every session sharing it. `MAX_SMILES_CHARS`
is the cheap pre-parse guard; **`MAX_MOLECULE_ATOMS` is the one that actually stops it**, because the
recursion depth scales with the atom count rather than the string length.

Both are read through `env_bound`, which was added so that a bound written as a magic number is one
nobody can loosen for a real megamolecule without editing code. `env_bound` takes a `minimum`, and
the argument for it is written out at length: a bound has no "off" setting, so `0` is a startup
failure rather than a pod that starts and refuses everything.

**The symmetric case was never considered.** `minimum` stops an operator turning a bound down until
it refuses everything. Nothing stopped them turning one *up* until it refuses nothing — and for the
bound whose job is to stop an uncatchable crash, those are not the same kind of mistake. Measured on
the container this was written on:

```
MCP_MAX_MOLECULE_ATOMS=999999999   # accepted at import, pod starts, readiness passes
MolToSmiles(MolFromSmiles("C" * 20000))  # exit 139
```

That is the exact denial of service the module docstring is written about, re-armed by the knob
provided to tune the guard against it — and the knob's own refusal message gave an operator no hint
that this one is different from a bound about taste.

A second defect of the same shape was found in `servers/unitops`. `shortcut_column` validates its
three compositions in two places and neither sees all three: `fenske_minimum_stages` checks
`x_B < x_D`, `underwood_minimum_reflux` checks `z < x_D`. Nothing checked `x_B < z`. The overall
balance `F·z = D·x_D + B·x_B` has no solution in positive `D` and `B` when both products are richer
in the light key than the feed, so the split is not difficult, it is impossible — and the tool
answered anyway. Measured before the guard, at alpha 2.5 with `z` 0.30, `x_D` 0.95, `x_B` 0.50:

```
ShortcutColumn(minimum_stages=3.21, minimum_reflux_ratio=1.99, theoretical_stages=7.26, ...)
```

An ordinary-looking design for a column nobody can build, against this repository's own
"refuse rather than approximate" rule.

## Decision

**`env_bound` takes an optional `maximum`, refused at import in the same words as the floor**, and it
is passed at exactly one site — the bound whose ceiling is a crash rather than a preference. The
refusal names the variable, the value and the ceiling, so the way back is in the message.

**The ceiling is derived from this process's own stack, not transcribed.** A number here would be a
claim about somebody else's `ulimit -s`. The threshold was measured by canonicalising `"C" * n` under
a reduced stack limit and reading the exit status:

| stack | survives | dies |
| --- | --- | --- |
| 1 MiB | 2,000 | 2,500 |
| 2 MiB | 4,000 | 4,500 |
| 8 MiB | 18,000 | 20,000 |

That is 2.0 to 2.4 atoms per KiB across an eightfold range, so the threshold is linear in the stack.
`ATOMS_PER_KIB_OF_STACK` is that slope halved, and `stack_safe_atom_ceiling` reads `RLIMIT_STACK`'s
soft limit and multiplies. On the 8 MiB stack this was written against the ceiling is 8,192 — four
times the shipped default and, driven, canonicalises cleanly.

**Where the derivation lands below the module's own default it is floored there and reported at
WARNING**, rather than clamped in silence or raised. A deployment that changed nothing must not newly
fail to start; but a container whose stack cannot carry the default is one whose default is genuinely
thin, and the only honest thing to do with that is put it where an operator reading the container log
meets it. Driven under `ulimit -s 1024`: ceiling 1,024, floored to 2,000, one WARNING naming the
stack size. This is the same choice Chemclaw3 makes when its compaction trigger floors.

**`shortcut_column` gains the third comparison**, before any of the three correlations runs, with a
message that names the balance rather than the inequality — an operator who transposed two streams
needs to be told which fact is violated, not which line rejected them.

## Consequences

- An operator who genuinely needs a larger molecule raises the *stack* (`ulimit -s`, or the
  container's), which is the thing that actually governs it, and the ceiling follows. Raising the
  bound alone is now refused with the reason.
- `MAX_SMILES_CHARS` keeps a floor and no ceiling, correctly: raising it alone cannot reach the
  segfault, because the atom bound is downstream of the parse and catches what gets through.
- `maximum` is optional and passed nowhere else. A bound whose job is taste keeps a floor and no
  ceiling, which is the common case and stays unchanged.
- One impossible distillation split that used to return seven theoretical stages now refuses. No
  achievable split changes: the guard is a strict inequality on the feed, and an ordinary demanding
  column (1% light key in the bottoms against a 40% feed) is untouched.

## What keeps it true

- `packages/mcp_server_kit/tests/test_limits.py::test_the_atom_bound_cannot_be_raised_past_what_the_stack_survives`
  drives the defect through the module's own import.
- `packages/mcp_server_kit/tests/test_limits.py::test_the_ceiling_is_derived_from_this_process_s_own_stack_not_transcribed`
  holds the derivation against `RLIMIT_STACK` rather than against a number in a file.
- `packages/mcp_server_kit/tests/test_limits.py::test_a_stack_too_small_for_the_default_keeps_it_and_says_so`
  holds the floor and the WARNING together, so a silent clamp fails.
- `packages/mcp_server_kit/tests/test_limits.py::test_a_bound_above_its_ceiling_is_refused_naming_the_variable_the_value_and_the_ceiling`
  and `test_a_bound_with_no_ceiling_accepts_anything_above_its_floor` hold both directions of the new
  parameter, so "refuses everything" cannot pass as "has a ceiling".
- `servers/unitops/tests/test_distillation.py::test_a_split_whose_overall_mass_balance_cannot_close_is_refused`,
  `::test_bottoms_exactly_at_the_feed_composition_are_refused_too` and
  `::test_an_ordinary_split_is_untouched_by_the_balance_guard` hold the third comparison in both
  directions.
