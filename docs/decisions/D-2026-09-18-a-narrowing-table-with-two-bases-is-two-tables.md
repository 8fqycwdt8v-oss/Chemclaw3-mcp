# D-2026-09-18-a-narrowing-table-with-two-bases-is-two-tables — A narrowing table with two bases is two tables

**Status:** accepted · **Date:** 2026-09-18 · **Commit:** the arithmetic in
`D-2026-09-18-a-gate-that-does-not-read-the-tests-does-not-read-the-ratchets` is re-measured on one
stated basis, and the decision that record took gains the guard it never had. A merged record is
never edited, so the corrections are here.

## What was wrong

That record's three-row table compares `--strict` against two narrowings, "same tree, same commit".
Same commit, yes. Not the same basis. Rows 1 and 2 are whole-tree figures; **row 3 is filtered to
the test tree**, and the row does not say so.

Re-measured at `0d58969` with the same `mypy`, whole-tree (`$(SRC) $(TESTS)`) and test-tree-only
(`$(TESTS)`) side by side:

| invocation | whole tree | test tree only | of which `unused-ignore` | `src/` errors |
| --- | --- | --- | --- | --- |
| `--strict` | 153 in 32 | 153 in 32 | 2 | 0 |
| `--strict --disable-error-code=no-untyped-def` | 115 in 27 | 115 in 27 | 2 | 0 |
| the above, also without `attr-defined` and `arg-type` | **75 in 34** | 75 in 34 | **51** | **29** |

Two things that measurement settles and the first reading of it did not.

**The figures are basis-independent, because mypy reports errors in modules it follows.** Listing
only `$(TESTS)` still reports 75 in 34 — the `checked N source files` line moves (141 against 305),
the error count does not. So row 3's 46/26 came from a run whose `src/` lines were *filtered out*
after the fact: 75 − 29 = 46 and 51 − 29 = 22, exactly. Rows 1 and 2 have **zero** `src/` errors, so
the same filtering is invisible in them, which is how one table came to hold two bases without
anybody noticing.

**The load-bearing argument survives and gets stronger, twice over.** The record's point is that
`--disable-error-code` and `warn_unused_ignores` fight — the narrowing manufactures the one finding
class with a demonstrated yield here. That is 22 against 2 as written and **51 against 2** measured
whole-tree. And 29 of the 51 are in `src/`: a narrowing proposed for the *test* tree turns 29 live
suppressions in **serving code** into noise, which is a cost the filtered row could not show at all.

The correct sentence for row 3 is **75 (46 in the test tree)**.

## The other two loose links, in the cost paragraph

The same record's "what the fix actually cost" chain is read as a measurement, and two of its links
are not one. Re-driven at `0d58969` against the same whole-tree invocation:

- "`servers/chem/tests/test_sites.py` reported 24 of them. `_by_atom` was annotated
  `dict[int, object]` where it returns `dict[int, Site]`. One line, plus an import: **153 → 131**."
  Applying exactly that edit (`git diff --numstat` → `2 2`) gives `Found 131 errors in 32 files`, so
  the arithmetic is right *about the annotation*. The file's 24 is **22 `attr-defined` plus 2
  `no-untyped-call`**, and the two the annotation does not touch are the pair
  `D-2026-09-18-a-suppression-nobody-argued-reads-as-a-reviewed-one` is about. Putting 24 and
  153 → 131 in one sentence reads as if the file's count were the delta; it is 22.
- "38 `no-untyped-def` were `def test_x():` with no `-> None`, applied mechanically from mypy's own
  note: **131 → 95**." With the annotation applied and `no-untyped-def` disabled the run reports
  `Found 93 errors in 27 files`, and 131 − 38 = 93 independently. So 95 is neither the arithmetic
  nor the disable. If it came from actually writing 38 annotations, then two errors appeared that
  "applied mechanically" does not account for and the record does not mention — which is the same
  reading failure as the row above, in the other direction.

Neither changes the decision, and both are recorded for the reason this repository already has a
record about: a figure in prose is a claim about a commit, and these are claims about a commit whose
tree is still here to be re-run.

## The decision this adds

The record decided "full `--strict` … with no check dropped and no configuration relaxed", and
**nothing held it**. A `disable_error_code` key in `[tool.mypy]`, or a `--disable-error-code` on the
recipe, would re-take the rejected narrowing silently — and the recipe half is exactly the gap
`D-2026-09-18-a-ratchet-that-observes-half-a-command-holds-half-a-gate` found one function away.
So both ends are now checked.

A per-module `ignore_missing_imports` is deliberately outside the rule: it records that a
third-party distribution ships no stubs, which is a fact about that distribution rather than a check
this repository declines.

## What keeps it true

- `tests/test_fleet.py::test_the_type_gate_narrows_no_check_it_was_argued_out_of` — `strict = true`
  in `[tool.mypy]`, no `disable_error_code` and no `warn_unused_ignores = false` in it or in any
  override, and no relaxing flag on the command `make -n type` prints.
- `tests/test_fleet.py::test_the_type_gate_reads_the_test_tree_and_not_only_the_source` — the other
  half of the same gate: what it is run *over*. A narrowing and an omission are the same outcome.
- The figures above are historical measurements of `0d58969` and nothing keeps a historical
  measurement true. What is checkable is the configuration they argued for, which is the first two
  citations.
