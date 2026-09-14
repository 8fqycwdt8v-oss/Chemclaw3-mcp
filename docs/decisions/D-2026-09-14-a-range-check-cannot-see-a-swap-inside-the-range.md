# D-2026-09-14-a-range-check-cannot-see-a-swap-inside-the-range — A range check cannot see a swap inside the range

**Status:** accepted · **Date:** 2026-09-14 · **Commit:** fix pass over W26/W27/W29, on top of
`f3f3c9c`. Revisits `D-2026-09-13-a-hand-compiled-rule-table-is-a-table-with-a-typo-in-it`, whose
audit and whose other three checks stand.

## Context

That record's whole premise is that **a hand-compiled table is a table with a typo in it**, and the
realistic typo it names is a transposition between two *real* element symbols, which a spell-check
of the list against itself cannot see. The test written for it says so in its own docstring:

> This is the check that catches a transposition between two **real** element symbols — `Rb` for
> `Rh`, `Ru` for `Cu`.

### Measured

`"Cu"` replaced by `"Ru"` in `TRANSITION_METALS` (`git diff --stat` → `1 file changed, 1 insertion,
1 deletion`):

```
$ uv run pytest servers/rxnlabel/tests/test_agent_tables.py -q
133 passed in 0.21s
```

Green. The predicate is `21 <= Z <= 30 or 39 <= Z <= 48 or 57 <= Z <= 80`, evaluated **per symbol
in the set**, so it can only see a symbol that has left the block. `Rb`(37) leaves it; `Ru`(44) for
`Cu`(29) does not, because both are inside. Copper silently disappearing from the catalyst table —
which is what that edit *is* — is invisible, and it is the failure the file exists for.

A second, smaller error in the same line: `57 <= Z` calls `La`–`Lu` d-block. They are the lanthanide
series. The bound would have accepted a lanthanide in a table of catalytic metals.

The general shape, which is what this record is for: **a predicate over a range answers a question
about each member and no question about the set.** A transposition inside the range is two events —
one symbol duplicated, another absent — and a per-member range test sees neither, because the
duplicate is swallowed by the `frozenset` before any test can count it and the absence is not a
member to iterate over.

## Decision

Hold the set by **membership against the periodic table, in both directions**, and hold the source
literal for duplicates separately.

- `test_the_metal_table_is_the_d_block_minus_the_one_argued_out` derives the d block from RDKit
  (Z 21–30, 39–48, **72**–80) and asserts set equality against `TRANSITION_METALS` plus the one
  argued exclusion, `Zn`. That makes an **absence** the failure, which is the half a range test
  cannot have. The exclusion is read from `DELIBERATELY_NOT_METALS`, the list that already carries
  it, so the two cannot drift; the test also asserts that `Zn` is the only d-block symbol in it, so
  quietly adding a second exclusion is a failure rather than a widening.
- `test_the_metal_set_literal_holds_no_symbol_twice` counts the string constants in the module's
  own AST against `len(TRANSITION_METALS)`. This is `test_no_two_solvent_tokens_are_the_same_molecule`'s
  shape, one table over, and it is the reason a set literal can be checked at all: `{"Ru", …, "Ru"}`
  is one element by the time anything can import it.
- The range test keeps its name and its per-symbol failure, its bound corrected to 72–80, and its
  docstring now says what it does and does not catch. **Its name is unchanged** because
  `D-2026-09-13-a-hand-compiled-rule-table-is-a-table-with-a-typo-in-it` cites it and a merged record
  is never edited.

`Cd` and `Hg` stay in and `Zn` stays out. That asymmetry is chemistry, it is argued in the comment
beside the set, and the previous record recorded it as observed rather than acted on; this change
holds the table as it stands rather than re-deciding it.

## What keeps it true

- `servers/rxnlabel/tests/test_agent_tables.py::test_the_metal_table_is_the_d_block_minus_the_one_argued_out`
  — driven on three mutations: `Cu`→`Ru` (a duplicate), `Cu`→`Zn` (no duplicate, and the range test
  still passes), and `La` added. Red on all three.
- `servers/rxnlabel/tests/test_agent_tables.py::test_the_metal_set_literal_holds_no_symbol_twice` —
  red on `Cu`→`Ru`, green on `Cu`→`Zn`, which is the discrimination that makes the two separate
  tests rather than one.
- `servers/rxnlabel/tests/test_agent_tables.py::test_every_transition_metal_symbol_is_a_d_block_element`
  — red on `La` with the corrected bound, where the shipped 57–80 accepted it.
- `servers/rxnlabel/tests/test_agent_tables.py::test_the_main_group_metals_the_comment_excludes_are_still_excluded`
  — unchanged, and the other direction on `Zn`.
