# D-2026-10-07-the-record-gets-lean — The record gets lean: decisions in ADRs, rules in CLAUDE.md, architecture in tests

**Status:** accepted · **Date:** 2026-10-07

## Context

The record here had become a changelog. Most files under `docs/decisions/` read as defect reports
rather than choices; `CLAUDE.md` restated their history at length; module docstrings carried the
same history a third time; and a family of tests policed the prose itself — counts, citations,
paths and phrases in documents — so a wording change could red the gate while the code it
described was untouched. Chemclaw3 took the same decision for itself as decision 5 of
`D-2026-10-07-the-architecture-programme`; this record adopts it for this repository, because a
decision there binds nothing here.

## Options

1. **Keep the record as it is** — every finding an ADR, history in `CLAUDE.md` and docstrings,
   prose policed by tests. Costs reading time on every turn and every review, and keeps the gate
   coupled to wording.
2. **Delete the history outright** — drop old ADRs and the tests that cite them. Loses the
   measurements behind rules that are still in force, and breaks every citation to a merged record.
3. **Lean record, history kept where it already is** — an ADR only for a choice between options or
   a decline; `CLAUDE.md` and docstrings state current rules (what and why), not how they arose;
   tests that police prose are retired while tests that hold the architecture stay; merged records
   are kept and never edited, except for an added `Superseded-by:` line.

## Decision

Option 3. A defect fix is a commit and a test. A record from 2026-10-07 on carries `## Options`,
and one that declines a class of work carries `Revisit when:`. `CLAUDE.md` is current rules only;
a docstring says what and why. The prose-policing tests are retired and listed by name in
`tests/test_decision_log.py`'s retired-citation table so the merged records that cite them still
resolve; the records about policing prose move under "Retired" in `CURRENT.md`. Architecture tests
(layout, manifests, deploy shape, egress, images, auth, data identity) stay. `Superseded-by:` is the
one permitted edit to a merged record.

## Consequences

A wording change no longer reds the gate, and a stale sentence is caught by review rather than by a
test. History is read from `git log` and the merged records, not from `CLAUDE.md`. Option 1's cost
is gone; option 2's loss is avoided.

**Revisit when:** a stale sentence in `CLAUDE.md` or a docstring misleads a change into a defect
that a retired prose test would have caught — recorded in the fixing commit's message, which is
what would justify reinstating that one test.

## What keeps it true

- `tests/test_decision_log.py::test_a_record_from_the_cursor_on_weighs_its_options`
- `tests/test_decision_log.py::test_every_test_a_record_names_still_exists`
- `tests/test_decision_log.py::test_no_record_carries_an_unresolved_conflict_marker`
