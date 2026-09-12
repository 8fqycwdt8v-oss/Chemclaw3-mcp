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
| [D-2026-09-12-a-bypass-that-is-not-in-the-suite-is-not-closed](D-2026-09-12-a-bypass-that-is-not-in-the-suite-is-not-closed.md) | A bypass that is not in the suite is not closed |
| [D-2026-09-12-a-ratchet-measures-what-it-parses](D-2026-09-12-a-ratchet-measures-what-it-parses.md) | A ratchet measures what it parses, not what it is named after |

[`Chemclaw3`]: https://github.com/8fqycwdt8v-oss/Chemclaw3
