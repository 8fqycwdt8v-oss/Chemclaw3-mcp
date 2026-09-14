# `docs/decisions/` — the record

Why this fleet is the way it is, one file per decision. A decision that has changed gets a **new**
record that supersedes the old one; a merged record is never edited, because it is right about the
moment it was written.

## The convention

**Name the file `D-YYYY-MM-DD-<slug>.md`** — today's date plus a slug naming the decision — give it
the heading `# D-YYYY-MM-DD-<slug> — Title`, and add its row to the table below. That is the whole
procedure: nothing to reserve, nothing to coordinate with another session.

The id is the **whole stem**, not the date. A second record on the same day is normal here, and an
id that names more than one decision is the failure this ledger exists to prevent. Two authors
collide only on the same date *and* the same slug, and even that arrives as an add/add conflict on a
filename, which git reports loudly.

**There is no numbered sequence here and there must never be one.** [`Chemclaw3`] has a frozen
`D-NNN` range because it started with one and the citations to it still have to resolve; its record
of why that ended (`D-2026-07-31-adr-ids-that-cannot-collide`) is the reason this repository skips
the stage: "highest on `origin/main`, plus one" is a read that is stale the instant another session
pushes, and many sessions run at once across this family. Allocating a number here would import a
solved problem. `tests/test_decision_log.py` rejects the shape outright, in both directions.

**There is no "By topic" index yet, deliberately.** A second index buys navigation and costs an
update nobody is reminded to make: the sibling's went stale by a whole month of records under four
green assertions about the record table beside it, because nothing checked it. It starts paying for
itself somewhere past a hundred records, when reading the table below stops being how you find the
current decision on a subject. Add it *with* the ratchet that fails when a new record lands unfiled
— not before, and not without.

**Every record ends with a `## What keeps it true` section naming the `test_*` that holds it.** That
section is the load-bearing half: it is what a later session reads to find out whether a claim is
still enforced, and `test_every_test_a_record_names_still_exists` resolves every name in it against
the suite, so a rename cannot retire a citation in silence. A record with nothing to name is a
record whose decision nothing enforces, and saying so is more useful than an empty heading.

**A number written here is a dated measurement of a named commit, never a claim about `HEAD`.** The
same rule `CLAUDE.md` applies to itself, for the same reason: the past does not move, so a
measurement attributed to a commit cannot go stale, while a bare figure is a claim about its
author's afternoon. Anything that must stay true belongs in a test, and the record cites it.

## The record

| Id | Title |
| --- | --- |
| [D-2026-09-12-a-bound-that-can-be-set-to-zero-has-to-say-what-zero-means](D-2026-09-12-a-bound-that-can-be-set-to-zero-has-to-say-what-zero-means.md) | A bound that can be set to zero has to say what zero means |
| [D-2026-09-12-a-bypass-that-is-not-in-the-suite-is-not-closed](D-2026-09-12-a-bypass-that-is-not-in-the-suite-is-not-closed.md) | A bypass that is not in the suite is not closed |
| [D-2026-09-12-a-ceiling-read-before-the-mint-is-a-ceiling-a-burst-walks-past](D-2026-09-12-a-ceiling-read-before-the-mint-is-a-ceiling-a-burst-walks-past.md) | A ceiling read before the mint is a ceiling a burst walks past |
| [D-2026-09-12-a-degradation-that-is-not-counted-is-a-degradation-nobody-sees](D-2026-09-12-a-degradation-that-is-not-counted-is-a-degradation-nobody-sees.md) | A degradation that is not counted is a degradation nobody sees |
| [D-2026-09-12-a-ratchet-measures-what-it-parses](D-2026-09-12-a-ratchet-measures-what-it-parses.md) | A ratchet measures what it parses, not what it is named after |
| [D-2026-09-12-a-raw-string-is-not-the-string-it-was-copied-from](D-2026-09-12-a-raw-string-is-not-the-string-it-was-copied-from.md) | A raw string is not the string it was copied from |
| [D-2026-09-12-a-readiness-check-that-does-not-run-the-thing-is-not-a-readiness-check](D-2026-09-12-a-readiness-check-that-does-not-run-the-thing-is-not-a-readiness-check.md) | A readiness check that does not run the thing is not a readiness check |
| [D-2026-09-12-a-session-is-memory-nobody-counted](D-2026-09-12-a-session-is-memory-nobody-counted.md) | A session is memory nobody counted |
| [D-2026-09-12-a-shared-helper-is-not-a-proof-it-was-applied](D-2026-09-12-a-shared-helper-is-not-a-proof-it-was-applied.md) | A shared helper is not a proof it was applied |
| [D-2026-09-12-a-test-that-re-types-the-expression-under-test-asserts-nothing](D-2026-09-12-a-test-that-re-types-the-expression-under-test-asserts-nothing.md) | A test that re-types the expression under test asserts nothing |
| [D-2026-09-12-an-assert-is-a-control-with-an-off-switch](D-2026-09-12-an-assert-is-a-control-with-an-off-switch.md) | An `assert` is a control with an off switch |
| [D-2026-09-12-one-tool-call-is-not-one-thread](D-2026-09-12-one-tool-call-is-not-one-thread.md) | One tool call is not one thread |
| [D-2026-09-12-whitespace-is-an-accident-on-the-side-that-provisions](D-2026-09-12-whitespace-is-an-accident-on-the-side-that-provisions.md) | Whitespace is an accident on the side that provisions |
| [D-2026-09-13-a-cache-key-derived-from-text-nobody-validated-is-not-a-key](D-2026-09-13-a-cache-key-derived-from-text-nobody-validated-is-not-a-key.md) | A cache key derived from text nobody validated is not a key |
| [D-2026-09-13-a-gate-in-another-system-is-not-a-gate-this-one-can-see](D-2026-09-13-a-gate-in-another-system-is-not-a-gate-this-one-can-see.md) | A gate in another system is not a gate this one can see |
| [D-2026-09-13-a-hand-compiled-rule-table-is-a-table-with-a-typo-in-it](D-2026-09-13-a-hand-compiled-rule-table-is-a-table-with-a-typo-in-it.md) | A hand-compiled rule table is a table with a typo in it |
| [D-2026-09-13-a-probe-that-can-kill-the-pod-is-not-a-readiness-probe](D-2026-09-13-a-probe-that-can-kill-the-pod-is-not-a-readiness-probe.md) | A probe that can kill the pod is not a readiness probe |
| [D-2026-09-13-a-suppression-with-no-expiry-outlives-its-argument](D-2026-09-13-a-suppression-with-no-expiry-outlives-its-argument.md) | A suppression with no expiry outlives its argument |
| [D-2026-09-13-an-audit-of-a-lockfile-no-image-reads-audits-nothing](D-2026-09-13-an-audit-of-a-lockfile-no-image-reads-audits-nothing.md) | An audit of a lockfile no image reads audits nothing |
| [D-2026-09-13-the-rule-that-would-have-caught-it-was-not-the-one-asked-for](D-2026-09-13-the-rule-that-would-have-caught-it-was-not-the-one-asked-for.md) | The rule that would have caught it was not the one asked for |
| [D-2026-09-14-a-child-process-is-outside-the-guard-and-uv-build-is-one](D-2026-09-14-a-child-process-is-outside-the-guard-and-uv-build-is-one.md) | A child process is outside the guard, and `uv build` is one |
| [D-2026-09-14-a-citation-a-squash-merge-retires-is-not-provenance](D-2026-09-14-a-citation-a-squash-merge-retires-is-not-provenance.md) | A citation a squash merge retires is not provenance |
| [D-2026-09-14-a-degraded-answer-is-counted-once](D-2026-09-14-a-degraded-answer-is-counted-once.md) | A degraded answer is counted once |
| [D-2026-09-14-a-depth-nobody-asserts-is-a-default-waiting-to-return](D-2026-09-14-a-depth-nobody-asserts-is-a-default-waiting-to-return.md) | A depth nobody asserts is a default waiting to return |
| [D-2026-09-14-a-layer-nobody-reads-fleet-wide-is-a-convention](D-2026-09-14-a-layer-nobody-reads-fleet-wide-is-a-convention.md) | A layer nobody reads fleet-wide is a convention |
| [D-2026-09-14-a-lint-rule-that-does-not-fire-is-not-the-control-it-was-read-as](D-2026-09-14-a-lint-rule-that-does-not-fire-is-not-the-control-it-was-read-as.md) | A lint rule that does not fire is not the control it was read as |
| [D-2026-09-14-a-range-check-cannot-see-a-swap-inside-the-range](D-2026-09-14-a-range-check-cannot-see-a-swap-inside-the-range.md) | A range check cannot see a swap inside the range |
| [D-2026-09-14-a-ratchet-holds-the-set-it-enumerates](D-2026-09-14-a-ratchet-holds-the-set-it-enumerates.md) | A ratchet holds the set it enumerates |
| [D-2026-09-14-a-ratchet-that-matches-a-comment-holds-nothing](D-2026-09-14-a-ratchet-that-matches-a-comment-holds-nothing.md) | A ratchet that matches a comment holds nothing |
| [D-2026-09-14-a-row-that-cannot-occur-proves-the-other-branch](D-2026-09-14-a-row-that-cannot-occur-proves-the-other-branch.md) | A row that cannot occur proves the other branch |
| [D-2026-09-14-a-spelling-list-is-not-a-derivation](D-2026-09-14-a-spelling-list-is-not-a-derivation.md) | A spelling list is not a derivation |
| [D-2026-09-14-a-total-beside-a-mutation-needs-the-invocation-that-produced-it](D-2026-09-14-a-total-beside-a-mutation-needs-the-invocation-that-produced-it.md) | A total beside a mutation needs the invocation that produced it |
| [D-2026-09-14-eight-assertions-of-one-clause-is-a-choice-not-an-accident](D-2026-09-14-eight-assertions-of-one-clause-is-a-choice-not-an-accident.md) | Eight assertions of one clause is a choice, not an accident |
| [D-2026-09-14-how-many-stayed-green-is-a-claim-about-a-commit](D-2026-09-14-how-many-stayed-green-is-a-claim-about-a-commit.md) | "How many stayed green" is a claim about a commit |
| [D-2026-09-14-one-question-gets-one-expression](D-2026-09-14-one-question-gets-one-expression.md) | One question gets one expression |
| [D-2026-09-14-the-gate-that-catches-a-change-is-the-gate-of-the-tree-it-is-made-in](D-2026-09-14-the-gate-that-catches-a-change-is-the-gate-of-the-tree-it-is-made-in.md) | The gate that catches a change is the gate of the tree it is made in |
| [D-2026-09-14-the-table-was-right-and-the-sentence-above-it-was-not](D-2026-09-14-the-table-was-right-and-the-sentence-above-it-was-not.md) | The table was right and the sentence above it was not |
| [D-2026-09-14-thirteen-ignored-is-duplication-not-aliasing](D-2026-09-14-thirteen-ignored-is-duplication-not-aliasing.md) | `13 ignored` is duplication, not aliasing |
| [D-2026-09-14-what-this-fleet-enforces-bounds-measures-and-accepts](D-2026-09-14-what-this-fleet-enforces-bounds-measures-and-accepts.md) | What this fleet enforces, bounds, measures and accepts |

[`Chemclaw3`]: https://github.com/8fqycwdt8v-oss/Chemclaw3
