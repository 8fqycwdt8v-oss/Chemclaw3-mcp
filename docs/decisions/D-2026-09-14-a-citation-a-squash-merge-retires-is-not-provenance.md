# D-2026-09-14-a-citation-a-squash-merge-retires-is-not-provenance — A citation a squash merge retires is not provenance

**Status:** accepted · **Date:** 2026-09-14 · **Commit:** post-merge fix pass over Wave 30, on top
of `24b50ec` (PR #66).

## Context

`main` is red. Not on a new check — on
`tests/test_decision_log.py::test_every_commit_the_registers_cite_is_reachable_from_head`, which
exists precisely to stop the thing that happened, and whose docstring is a verbatim description of
it: *"PR #54 was **squash**-merged, so none of the four is an ancestor of `main` and the command
fails for anyone who clones it."*

Wave 30's two records both name the branch commit as their provenance:

```
D-2026-09-14-the-gate-that-catches-a-change-…md:3   **Commit:** Wave 30.5, `eb58363`.
D-2026-09-14-what-this-fleet-enforces-…md:3         **Commit:** Wave 30.8, on top of `eb58363`.
```

`eb58363` is the branch head PR #66 squashed into `24b50ec`. So the sign-off wave — the one whose
own subject is what this fleet can and cannot check — reproduced, in its own two records, the defect
the test beside it was written for. Measured on a full clone at `24b50ec`:

```
$ git rev-parse --is-shallow-repository
false
$ .venv/bin/python -m pytest tests/ -q
FAILED tests/test_decision_log.py::test_every_commit_the_registers_cite_is_reachable_from_head
1 failed, 232 passed, 1 skipped
```

### Why nobody saw it, which is the larger half

The check has an honest escape hatch: on a shallow clone `git merge-base --is-ancestor` cannot
decide reachability, so it warns and returns rather than guessing. All three jobs in
`.github/workflows/ci.yml` used `actions/checkout` with no `fetch-depth:` — the default is depth 1,
which is shallow. **The assertion had therefore never run in CI and could not.** It fires only for a
developer with full history, where it has been permanently red since the merge.

That is a control proven nowhere, which is exactly the genre
`D-2026-09-14-what-this-fleet-enforces-bounds-measures-and-accepts` §4 exists to enumerate, and §4
does not list it. The gate was green on a merge that broke the gate.

### A third defect, in the message rather than the check

`cited` was built as `{commit: path.name}`. Two records citing one hash collapse onto one key, so
the failure named one file and hid the other — a reader who fixed the file the message named would
have been left with a red run and no clue which record was still wrong. Here that is exactly the
case: both W30 records cite `eb58363` and only the second was reported.

## Decision

**Three changes, one per defect.**

1. **The reachable citation for both Wave-30 records is `24b50ec`, PR #66.** They are merged, so
   they are not edited; this record is the correction, and it is what a reader who follows
   `eb58363` to nothing is meant to find.

   The exemption that lets the suite go green is deliberately *not*
   `_QUOTED_AS_UNREACHABLE`. That set means "a record names this hash because the point is that it
   does not resolve", which is true of PR #54's four and false of these two — they were written as
   plain provenance and were true on the branch they were written on. `_RETIRED_BY_A_SQUASH` is a
   map from the retired hash to the record that supplies the reachable citation in its place, and it
   is checked in three directions: the hash must stay unreachable, the named record must exist, and
   that record must itself cite a commit `HEAD` reaches. An exemption cannot outlive the correction
   that earns it, and it cannot be a place a stale citation goes to be forgotten.

2. **`fetch-depth: 0` on the two checkouts whose jobs run the suite** (`check` and `offline`), with
   the reason at the site. `deps` keeps the default and says why: it runs `make deps-audit` and no
   test, so it has nothing to decide about ancestry.

   The consequence is the point. `on: push: branches: ["**"]` includes `main`, so from now on the
   push that lands a squash merge runs this assertion against the merged history — and a record that
   cites its branch commit reds `main` immediately and loudly, instead of waiting for someone to
   clone with full history. The recurrence is not prevented; it is made *visible*, which is the only
   thing a check can do about a hash that does not exist yet when the record is written.

3. **The failure names every record that cites a bad hash.** `cited` is `dict[str, set[str]]` and
   the message is generated per (record, commit) pair.

### What this does not do

It does not stop the next wave writing its branch hash — that hash is genuinely the only one its
author can know. What changes is the delay between doing it and finding out: it was "until a
developer happens to clone deeply", it is now "the CI run on the merge commit".

## What keeps it true

- `tests/test_decision_log.py::test_every_commit_the_registers_cite_is_reachable_from_head` — the
  check itself, now with the second exemption and the per-record message. Driven: with
  `_RETIRED_BY_A_SQUASH` pointing at a record that does not exist, it fails on the new `uncorrected`
  assertion naming the missing stem; with this record present and citing `24b50ec`, it passes; with
  `24b50ec` replaced by a hash `HEAD` cannot reach, it fails again.
- `.github/workflows/ci.yml` — `fetch-depth: 0` on `check` and `offline`. Not test-enforced, and
  deliberately so: a test asserting the content of the file that runs it is a control reading its
  own configuration. What holds it is that the assertion is now non-vacuous in CI, so removing the
  depth silently turns a red gate green — which is the failure mode this record documents, and the
  next reviewer to drive it will find it the same way this one did.
