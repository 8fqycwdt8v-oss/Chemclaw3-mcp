# D-2026-09-14-a-row-that-cannot-occur-proves-the-other-branch — A row that cannot occur proves the other branch

**Status:** accepted · **Date:** 2026-09-14 · **Commit:** post-merge fix pass over `14a764b`
(PR #67), on top of `e8cf74a` (PR #68).

## Context

`D-2026-09-14-a-ratchet-holds-the-set-it-enumerates` widened four readers that each stood in for a
larger property than they enumerated, and held each widening with a bite test on a synthetic
subject. The fourth of those — `inert_outcome`, which decides whether a green consumer run asserted
anything about this tree — is right, and **its bite test did not hold it**.

Measured on `e8cf74a`, gutting `_INERT_OUTCOMES` from
`("skipped", "xfailed", "xpassed", "deselected")` back to `("skipped",)` — restoring exactly the
blindness the widening exists to remove:

```
$ sed -i 's/^_INERT_OUTCOMES = ("skipped", "xfailed", "xpassed", "deselected")$/_INERT_OUTCOMES = ("skipped",)/' tests/test_consumer_agreement.py
$ git diff --numstat tests/test_consumer_agreement.py
1	1	tests/test_consumer_agreement.py
$ uv run pytest -q tests/test_consumer_agreement.py -p no:cacheprovider
3 passed in 7.71s            # GREEN
```

The cause is that `inert_outcome` has two arms and the table exercised the wrong one. Five of its
six rows carried **no `passed` count**, so each was decided by `if not counts.get("passed")` and
never reached `_INERT_OUTCOMES` at all:

```
row                             SHIPPED                  _INERT_OUTCOMES = ("skipped",)
'1 xfailed in 0.31s'            reported 1 xfailed       ran no test that passed   <- still not None
'1 xpassed in 0.31s'            reported 1 xpassed       ran no test that passed   <- still not None
'2 deselected in 0.02s'         reported 2 deselected    ran no test that passed   <- still not None
'1 passed, 1 skipped in 0.3s'   reported 1 skipped       reported 1 skipped        <- the only bite
'no tests ran in 0.01s'         ran no test that passed  ran no test that passed
''                              ran no test that passed  ran no test that passed
```

So the table proved "did anything pass?" four times over and the widening not once, while its own
docstring said "The `1 xfailed` row is the one that mattered". A lone `1 xfailed` is also the one
shape that **cannot occur** for this guard's actual subject: it runs `Chemclaw3`'s
`tests/test_sibling_manifest_agreement.py`, which has three tests, so an `xfail` added to one of
them reports `2 passed, 1 xfailed`.

This is the record's own subject arriving one level up, inside the commit that recorded it: a reader
that agrees with a weaker implementation forever, this time the reader being the test.

## Decision

**Every row of the table carries a `passed` count.** A row without one is answered by the other arm
and therefore says nothing about the set — the two no-pass rows kept (`no tests ran`, and the empty
string) are there to hold *that* arm deliberately, and the lone-inert rows that held neither are
gone rather than kept alongside. The rows are the shapes that occur: a pass count beside the inert
outcome.

`_INERT_OUTCOMES` itself is unchanged; `D-2026-09-14-a-ratchet-holds-the-set-it-enumerates`'s
decision stands in full. What changes is that it is now held.

## What keeps it true

- `tests/test_consumer_agreement.py::test_an_inert_consumer_run_is_not_agreement` — driven by
  dropping each member of `_INERT_OUTCOMES` in turn (`xfailed`, `xpassed`, `deselected`, `skipped`),
  numstat `1 1` each: **all four red**, where before this commit only `skipped` did. The whole-set
  mutation to `("skipped",)` reds on the `2 passed, 1 xfailed` row.
