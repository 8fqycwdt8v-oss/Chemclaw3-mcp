# D-2026-09-13-a-gate-in-another-system-is-not-a-gate-this-one-can-see — A gate in another system is not a gate this one can see

**Status:** accepted · **Date:** 2026-09-13 · **Commit:** wave W29, on top of `0fb12537`.
No hash is written for this pass, for the reason
`D-2026-09-12-a-bypass-that-is-not-in-the-suite-is-not-closed` §5 gives.

## Context

Two defects in the two pipelines, both of the same shape: a control that is somewhere else.

**`Jenkinsfile`: `RUN_GATE` defaults to `false`**, described as "Off because GitHub Actions is the
gate". That is a true sentence about a different system, and **nothing in the pipeline ever looked
at it** — no step reads a check run, a status or a conclusion for `env.REVISION`. So an image could
be built and published *by digest* from a revision whose `make check` had never run, or had run
red, and the release descriptor that digest lands in carries no trace of which.

**`ci.yml`: the `Lint` step spelled `uv run ruff check . && uv run ruff format --check .` inline**,
a second declaration of what the gate is — the exact defect the comment three lines below it
describes being found and fixed for `make type`, where the CI step had drifted to three source
trees while the Makefile named four and `servers/safety/src` was type-checked only locally.

**`ci.yml`: the `manifests` job ran `uv run pytest -q tests`**, and `testpaths` is
`["tests", "packages", "servers"]` — so all 182 of its tests were already among the 2,034 the
`check` job runs, on the same image, after the same `uv sync`.

## Decision

**Publishing requires the local gate.** `Preflight` refuses when a run would publish
(`!DRY_RUN` *and* a registry) with `RUN_GATE` off, naming the two ways forward. A dry run still does
not need it: a build that ships nothing is allowed to be fast, which is what the parameter was for.

Asking GitHub for the revision's check status was considered and rejected. It needs a credential
this agent does not have, and it makes a publish depend on a third party being reachable — a new
failure mode in the one pipeline that has to work during an incident. What is decidable *here* is
the local gate, so publishing requires that instead.

**`ci.yml`'s `Lint` step calls `make lint`,** and the `Tests` step calls `make cov` — same suite plus
the coverage floor (`D-2026-09-13-the-rule-that-would-have-caught-it-was-not-the-one-asked-for`).

**The `manifests` job is deleted.** A job that cannot go red unless another job also goes red is a
second light on one fault: it bought a runner's worth of minutes per push and a second place to read
the same failure. Nothing is lost — the fleet-level invariants it named *are* `tests/`, and `check`
runs them. If fast feedback on them is ever wanted, that is a step order inside `check`, not a
duplicate job.

## What keeps it true

- `tests/test_delivery.py::test_a_publishing_run_cannot_skip_the_gate` — three facts, because the
  refusal is wrong if any one is missing and each fails differently: the guard names all three
  conditions (dropping the registry term would refuse a build-only run), it `error`s rather than
  warns (a pipeline that logs and continues has published by the time anybody reads the log), and
  the `Gate` stage it sends an operator to still runs `make check`. Driven by mutation in the first
  two directions.
- `Makefile`'s `lint`, `type` and `cov` targets are the single declaration the CI steps now call.
  Nothing tests that a workflow step calls a target — a test reading YAML for a string is the
  weakest thing in this record, and the stronger control is that there is one list to drift from.
