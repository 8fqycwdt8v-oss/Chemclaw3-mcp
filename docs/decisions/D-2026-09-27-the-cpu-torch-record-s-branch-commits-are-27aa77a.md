# D-2026-09-27-the-cpu-torch-record-s-branch-commits-are-27aa77a — The CPU-torch record's branch commits are `27aa77a`

**Status:** accepted · **Date:** 2026-09-27 · **Commit:** on top of `27aa77a` (PR #132).

## Context

`main` is red on
`tests/test_decision_log.py::test_every_commit_the_registers_cite_is_reachable_from_head`, the check
`D-2026-09-14-a-citation-a-squash-merge-retires-is-not-provenance` made non-vacuous in CI so that
this exact recurrence would be loud rather than latent. It was.

`D-2026-09-27-a-cpu-pod-locks-the-cpu-torch` cites two commits as the provenance of its image-size
measurements:

```
D-2026-09-27-a-cpu-pod-locks-the-cpu-torch.md:14   image was built at `64201bd`, which is `main` plus that step
D-2026-09-27-a-cpu-pod-locks-the-cpu-torch.md:53   … on this change's pull request (#132, at `b812a29`)
```

Both were true on the branch they were written on. PR #132 was **squash**-merged into `27aa77a`, so
neither is an ancestor of `main`, and every pull request merged against `main` since has inherited
the red check — PR #138 among them, on a change that touches no record.

## Decision

**The reachable citation for both is `27aa77a`, PR #132.** The record is merged, so it is not
edited; this record is the correction, and it is what a reader who follows `64201bd` or `b812a29`
to nothing is meant to find. The measurements themselves are unchanged: they were produced by the
`images` job of CI run 36297650767 (`64201bd`) and of PR #132's own pull-request runs (`b812a29`),
whose logs GitHub keeps against the run ids, not against the branch commits.

Both hashes enter `_RETIRED_BY_A_SQUASH` naming this record, under the same three-way check the
first entry is held to: the hash must stay unreachable, this record must exist, and this record must
itself cite a commit `HEAD` reaches.

The rule `D-2026-09-14-a-citation-a-squash-merge-retires-is-not-provenance` states still holds and
is not tightened here: a branch hash is the only one its author can know when a record is written,
and the check makes the lapse visible on the merge commit's run — which is what happened.

## What keeps it true

- `tests/test_decision_log.py::test_every_commit_the_registers_cite_is_reachable_from_head` — green
  with both entries present and this record citing `27aa77a`; red again if either entry is removed,
  if this record is deleted, or if `27aa77a` is replaced by a hash `HEAD` cannot reach.
