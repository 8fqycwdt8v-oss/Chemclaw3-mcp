# D-2026-09-14-how-many-stayed-green-is-a-claim-about-a-commit — "How many stayed green" is a claim about a commit

**Status:** accepted · **Date:** 2026-09-14 · **Commit:** post-merge fix pass over `14a764b`
(PR #67), on top of `e8cf74a` (PR #68).

## Context

`D-2026-09-14-a-ratchet-holds-the-set-it-enumerates` records its first drive as:

> Driven: neutering `_is_inert` reds it while the other 115 stay green.

Re-measured on `e8cf74a`, neutering the same predicate (numstat `1 1`):

```
$ uv run pytest -q tests/test_fleet.py -p no:cacheprovider
1 failed, 116 passed, 2 warnings in 5.14s
```

**116, not 115.** The number is not wrong about the drive — it is the count from *before* the same
commit added two test functions to `tests/test_fleet.py`, so the record was stale against its own
diff on the day it was written. That is exactly the shape
`D-2026-09-14-a-citation-a-squash-merge-retires-is-not-provenance` is about, two bullets up in the
same section, and the shape `CLAUDE.md` refuses for `docs/BACKLOG.md` and this ledger: a count of a
set that changes is a claim about its author's afternoon.

The figure is also the *least* interesting half of the measurement. `grep -n "stay green" docs/`
finds it in more than one record, and in each the property being claimed is the same one: the
mutation reds the test named and nothing else. How many tests happened to be collected beside it is
a function of every unrelated parametrisation in the file.

## Decision

**A driven mutation is recorded by what it reds, not by how many stayed green.** "Reds
`test_a_suppressed_test_is_not_a_proof` and no other test in the file" is the claim; a total is a
number no test holds and that the next commit to the file falsifies. Where a drive genuinely turns
on a count — a parametrised id, a number of files a reader returns — the record says which
*measurement* produces it, so a reader can re-run it.

The earlier records are not edited. Their conclusions are untouched: the mutation does red the test
each names, and it does red nothing else. Only the totals beside them were current at the commit
that wrote them, and this record is where a reader learns not to read one as live state.

## What keeps it true

- `tests/test_fleet.py::test_a_suppressed_test_is_not_a_proof` — the test the superseded figure was
  about. Driven at `e8cf74a` by neutering `_is_inert` (numstat `1 1`): it is the only failure in the
  file, which is the property the record meant and the part that does not go stale.
- `tests/test_decision_log.py::test_every_test_a_record_names_still_exists` — what this repository
  *can* hold about a record's prose: that the tests it names resolve. It cannot hold a total, which
  is the reason a record should not state one.
