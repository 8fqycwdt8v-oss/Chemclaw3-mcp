# D-2026-09-26-gestis-is-not-a-source-and-the-reason-outlives-the-build — GESTIS is not a source, and the reason outlives the build

**Status:** accepted · **Date:** 2026-09-26

## Context

`ghs` is proposed in `MODULES.md` (port 8870, not yet built) on a PubChem LCSS extract and ECHA
C&L. GESTIS, the IFA's substance database, is the most complete occupational hazard source there
is, and its terms prohibit transfer into other information systems — which a vendored corpus is.
Until this record the prohibition lived in two places: the `ghs` row of the catalogue and an open
question beneath it. `docs/BACKLOG.md` queued the problem with that: a hazard corpus is exactly the
kind of thing a later contributor "improves" by reaching for the most complete source available,
and a catalogue is prose nobody reads at the moment they add a source to a corpus.

## Decision

**Declined: GESTIS is not a source for any corpus in this fleet**, not for `ghs` and not for any
other server that grows hazard data. `ghs` is built on PubChem LCSS and ECHA C&L. The terms, not the
completeness, decide what may be vendored — the same posture `CLAUDE.md` takes on a corpus with no
recorded licence.

What makes the reason survive the build is that it now sits in three places a builder meets, rather
than only in the catalogue:

- **`docs/adding-a-server.md`, under "The dataset"** — the checklist every new server is written
  against — states that a source whose terms forbid redistribution is not a source, names GESTIS as
  the standing case, and requires the server that vendors a hazard corpus to say in its own README
  which source it may not use and why.
- **A fleet test reads every vendored `dataset.json`**, and a `retrieved_from` or `licence` naming
  GESTIS fails the suite.
- **The same test requires `servers/ghs/README.md` to name GESTIS the day that directory exists.**
  That half is vacuous now and is written now on purpose: building the server is what makes it bite,
  so the reason cannot be left behind in the catalogue when the server moves out of it.

**Revisit when:** `ghs` is built — its README carries the prohibition then, and this record is what
says why it must — or when GESTIS's terms of use change in writing.

## What keeps it true

- `tests/test_fleet.py::test_no_corpus_is_vendored_from_gestis_and_a_built_ghs_says_why` — no
  shipped corpus names GESTIS in its provenance, and a built `ghs` names it in its README.
