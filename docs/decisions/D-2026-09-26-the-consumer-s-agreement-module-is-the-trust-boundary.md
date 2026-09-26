# D-2026-09-26-the-consumer-s-agreement-module-is-the-trust-boundary — The consumer's agreement module is the trust boundary

**Status:** accepted · **Date:** 2026-09-26

## Context

`tests/test_consumer_agreement.py::test_the_consumer_still_agrees_with_the_surface_this_tree_declares`
runs whatever module sits at `AGREEMENT_MODULE` (`tests/test_sibling_manifest_agreement.py`) in a
Chemclaw3 checkout and reads its outcome. `inert_outcome` refuses a skip, an xfail, an xpass, an
empty selection and a run with no pass in it, which closes every way that module can be *inert*. It
does not close a module that genuinely passes and asserts nothing about this tree: driven
2026-09-14 at `e8cf74a`, a synthetic checkout whose whole agreement module was one `test_*` with
`assert True` gave `3 passed` here with the guard green.
`D-2026-09-14-what-this-fleet-enforces-bounds-measures-and-accepts` §4.6 names the *deletion* case
only, which is one instance of this. It is inherent to running the consumer's module rather than
reproducing it — reproducing it is the divergent copy the arrangement exists to avoid.

Three responses were queued in `docs/BACKLOG.md`: require a minimum pass count derived from the
consumer's file; name the expected tests by `--collect-only`; or name the consumer's file as the
trust boundary in the record.

## Decision

**The consumer's agreement module is trusted, and this record is where that is said.** Anything
that module asserts is what "agreement" means; this side guarantees only that it ran, found tests,
and passed them.

- **A minimum pass count is rejected**, because it couples the two trees' test counts and the
  coupling drifts on every ordinary change. The module this side measured at three checks on
  2026-09-14 held ten `test_*` functions on 2026-09-26, read at Chemclaw3's main (commit 2f836d59
  there, its PR #455): each addition over there would have needed a matching edit here, or the
  floor would have silently stopped meaning anything — and a floor of three would still pass a
  module reduced to three `assert True`s.
- **Naming the expected tests by `--collect-only` is rejected** for the same reason in a stronger
  form: it is a second declaration of the consumer's test names, in a repository that cannot see
  them change, which is the deleted port table `CLAUDE.md` keeps refusing, one repository out. It
  also proves only that the names exist, not that they assert anything.
- **The boundary is honest because the same owner reviews both repositories.** A module hollowed
  out over there is a pull request against Chemclaw3 that the owner of this fleet reviews; the
  place to catch it is that review, where the diff shows the assertions going, not a count here that
  cannot see what a test asserts. The guard's job is to catch *inert*, which is what can happen
  with nobody deciding it; hollow requires somebody to write it, and that is a review's question.

**Revisit when:** the two repositories stop sharing a reviewing owner — a `CODEOWNERS` on either
side naming a different team, or Chemclaw3 moving to another organisation — or a hollowed
agreement module is found to have merged there. Either removes the premise that makes the file a
safe boundary.

## What keeps it true

- `tests/test_consumer_agreement.py::test_an_inert_consumer_run_is_not_agreement` — every inert
  outcome is refused, which is the whole of what this side guarantees.
- `tests/test_consumer_agreement.py::test_the_consumer_still_agrees_with_the_surface_this_tree_declares`
  — the run itself, against the path `AGREEMENT_MODULE` names. Nothing tests that the consumer's
  module asserts anything about this tree; that is the boundary this record names, held by review.
