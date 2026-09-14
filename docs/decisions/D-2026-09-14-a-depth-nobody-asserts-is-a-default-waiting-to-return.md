# D-2026-09-14-a-depth-nobody-asserts-is-a-default-waiting-to-return — A depth nobody asserts is a default waiting to return

**Status:** accepted · **Date:** 2026-09-14 · **Commit:** post-merge fix pass over `14a764b`
(PR #67), on top of `e8cf74a` (PR #68). Supersedes the second "What keeps it true" bullet of
`D-2026-09-14-a-citation-a-squash-merge-retires-is-not-provenance`.

## Context

That record put `fetch-depth: 0` on the two `.github/workflows/ci.yml` jobs that run the suite,
because `test_every_commit_the_registers_cite_is_reachable_from_head` **warns and returns** in a
shallow clone — so under `actions/checkout`'s default depth of 1 the citation check had never run in
CI and could not. The depth is right. The reason given for not asserting it is not:

> Not test-enforced, and deliberately so: a test asserting the content of the file that runs it is a
> control reading its own configuration. What holds it is that the assertion is now non-vacuous in
> CI, so removing the depth silently turns a red gate green.

**This repository had already settled that question the other way, in the wave before.**
`tests/test_delivery.py` reads `Jenkinsfile` and asserts eight properties of it,
`test_a_publishing_run_cannot_skip_the_gate` among them — a test asserting the content of a CI
definition that governs whether the gate runs at all. Its docstring gives the reason: *"A Jenkinsfile
is checked by no compiler and no linter in this repository."* Neither is `ci.yml`. And the record
behind that test is `D-2026-09-13-a-gate-in-another-system-is-not-a-gate-this-one-can-see`, which is
this argument under a different subject, resolved in favour of reading the file. Measured:

```
$ grep -rn "fetch-depth\|is-shallow" tests/ packages/ scripts/ --include=*.py
tests/test_decision_log.py:291:        ["git", "rev-parse", "--is-shallow-repository"],
```

Nothing read `ci.yml`. Nothing anywhere in this tree did.

**And the fallback offered in its place is only true while a citation is already stale.** In steady
state — no stranded hash, the normal case — the reachability assertion passes with or without the
depth, so removing `fetch-depth: 0` produces *no signal at all* until the next squash merge strands
a hash. At that point CI is silently green and `main` is red for anyone who cloned with full
history, which is `14a764b`'s own defect recurring, undetected. The same silence swallows a fourth
job added later that runs the suite under the default depth.

## Decision

The depth is asserted where the eight other delivery properties are asserted, in
`tests/test_delivery.py`, and **derived from the jobs rather than written as a list of two**: every
job with a step running `make cov`, `make test`, `make check`, `make offline-run` or
`offline_check.py` must have `fetch-depth: 0` on every `actions/checkout` step, and must have one at
all. A job that runs no test — `deps`, which runs only `make deps-audit` — is not asked for history
and does not get the assertion.

What this deliberately does not reach, stated because the control otherwise reads as wider than it
is: `Jenkinsfile`'s `Gate` stage runs `make check` and `make offline-run` on the implicit
declarative checkout, whose depth is controller configuration outside this tree. **GitHub Actions is
the only place the reachability assertion is known to run**, and nothing here can change that.

## What keeps it true

- `tests/test_delivery.py::test_every_job_that_runs_the_suite_checks_out_full_history` — driven
  four ways on `.github/workflows/ci.yml`, each verified applied by numstat: deleting the `with:`
  block from the `offline` job (`0 2`) reds it; `fetch-depth: 1` on `check` (`1 1`) reds it;
  deleting `check`'s whole checkout step (`0 3`) reds it on the missing-checkout arm; and appending
  a fourth job that runs `make test` with a default-depth checkout (`7 0`) reds it naming `fourth`.
  The shipped file passes, and the `deps` job — no suite, no depth — is correctly not asked.
- `tests/test_decision_log.py::test_every_commit_the_registers_cite_is_reachable_from_head` — the
  assertion the depth exists to make non-vacuous, unchanged.
