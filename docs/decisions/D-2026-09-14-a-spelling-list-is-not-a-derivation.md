# D-2026-09-14-a-spelling-list-is-not-a-derivation — A spelling list is not a derivation

**Status:** accepted · **Date:** 2026-09-14 · **Commit:** fix pass over the adversarial review of
`811d3de`. Supersedes nothing; it corrects one claim in
`D-2026-09-14-a-depth-nobody-asserts-is-a-default-waiting-to-return`, which is merged and therefore
not edited.

## Context

`tests/test_delivery.py::test_every_job_that_runs_the_suite_checks_out_full_history` binds every CI
job that runs this repository's suite to `fetch-depth: 0`, because
`test_every_commit_the_registers_cite_is_reachable_from_head` warns and returns on a shallow clone —
a control that does not fail but does not *run*.

The record that added it, and the docstring it shipped with, both stated the reach the same way:

> Derived from the jobs rather than from a list of two: a fourth job that runs the suite is bound
> the day it is added, which is what a list of names cannot do.

Half of that is true. The *jobs* are read out of the workflow, so a new job is seen. What the job
**runs** was matched against `_SUITE_COMMANDS`, a tuple of five spellings — four `make` targets and
`offline_check.py` — so a job binding itself was a job that happened to spell its step one of five
ways.

### Measured

A fourth job appended to `.github/workflows/ci.yml` at `actions/checkout`'s default depth, whose
only step is `run: uv run pytest -q tests` (`git diff --numstat` → `7 0`):

```
$ uv run pytest -q tests/test_delivery.py
9 passed in 0.05s
```

Green, with a job running the whole suite on a depth-1 checkout — exactly the state the assertion
exists to refuse. And the spelling is not invented: it is the step the deleted `manifests` job ran,
quoted verbatim in this workflow's own comment at `.github/workflows/ci.yml:155`.

All four drives recorded for the original assertion used spellings already inside the tuple, which
is why none of them found this. A mutation drawn from the set a check enumerates tests the check's
arithmetic and never its reach.

## Decision

`_SUITE_COMMANDS` carries the bare runner name, `pytest`, beside the four `make` targets. A job that
invokes the runner directly — however it spells the wrapper around it — now binds.

The docstring stops claiming a derivation it does not perform. It says what is derived (the jobs)
and what is matched (the command), and names the tuple's comment for the drive that made the bare
name necessary. This is deliberately not a rewrite of the check into something that "derives"
whether a step runs the suite: nothing short of running the step can decide that, `make` targets are
this repository's own vocabulary and `pytest` is the runner they all reach, so the honest form is a
list that says it is one.

`deps` is still correctly outside the set — it runs `make deps-audit` and no test, and its own
comment says why it carries no depth.

## What keeps it true

- `tests/test_delivery.py::test_every_job_that_runs_the_suite_checks_out_full_history` — driven with
  the fourth job in place: `1 failed, 8 passed`, on `assert None == 0`, which is the missing
  `fetch-depth`. With the shipped `ci.yml` restored, `9 passed`, and the matched set is
  `['check', 'offline']`.
