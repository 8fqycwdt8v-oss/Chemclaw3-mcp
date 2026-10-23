# D-2026-10-23-chembl-is-vendored-only-as-share-alike-data-after-legal-review — ChEMBL is vendored only as share-alike data, after legal review

**Status:** proposed · **Date:** 2026-10-23 · **Answers:** the `docs/BACKLOG.md` row *"ChEMBL is
CC-BY-SA; `chembl` waits on what that obliges for a derived answer"*

**This record is not legal advice and settles no legal question.** It lays out what the licence
text says, which questions it leaves open for this fleet's use, and the build that would follow if
counsel confirms the reading below. Nothing is vendored on the strength of this record alone.

## Context

`chembl` (port 8862) is `proposed` in `MODULES.md` — `search_by_structure`, `bioactivities`,
`target_lookup` over a vendored ChEMBL slice — and has waited since it was catalogued on one
question: what the ChEMBL licence obliges once its data sits in this repository, in a server image,
and in an answer the agent quotes. Re-probed at `HEAD`: no `servers/chembl/` exists, no
`dataset.json` names ChEMBL, and `MODULES.md` still carries the warning. Every vendored corpus so far
is CC0 first-party content, so this would be the first third-party copyleft corpus in the fleet,
beside MIT-licensed code (`LICENSE`).

What the licence states, as published by EMBL-EBI for ChEMBL releases (to be re-read from the exact
release that would be vendored, since terms are per release and this machine has no network):

- ChEMBL is distributed under **Creative Commons Attribution-ShareAlike 3.0 Unported**
  (CC BY-SA 3.0), with a requested citation of the ChEMBL database paper.
- **Attribution (BY):** whoever distributes the work or an adaptation must keep the copyright
  notice, credit the source as the licensor asks, name the licence or link to it, and say that
  changes were made.
- **ShareAlike (SA):** an *Adaptation* that is *distributed or publicly performed* must be under
  the same or a compatible licence. SA attaches to the database and to adaptations of it, and is
  triggered by distribution, not by internal use.
- 3.0 Unported does not license the EU *sui generis* database right as 4.0 does, so in the EU the
  position on extracting a substantial part of the database rests on the licence's general grant
  and on national law.
- Individual facts (an IC50, a target name) are generally not copyrightable as such; the selection
  and arrangement of the database, and in the EU the database right, are what is licensed.

What that leaves open for this fleet, each a question for counsel:

1. **Is a vendored slice an Adaptation?** A filtered, re-keyed subset in a SQLite file almost
   certainly is; if so, the slice file is CC BY-SA 3.0 whatever this repository's licence is.
2. **Is the fleet image, or this repository, "distributed"?** If both stay internal to one legal
   entity, SA may never trigger; a repository or image shared with a partner or published would
   trigger it for the slice (not for the MIT code beside it, which is not an adaptation of it).
3. **Is an answer an Adaptation?** One tool answer quoting a handful of activity values is far from
   the database; an answer store that accumulates many (`Chemclaw3`'s D-011 result cache, knowledge
   notes citing them) may approach a substantial part. Where that line sits is the open question.
4. **Does the agent's derived conclusion inherit SA?** A chemist's decision informed by a value is
   not a copy of it; a note that reproduces tables of values may be.

## Options

1. **Decline `chembl`, as GESTIS was declined** — no copyleft corpus in the fleet. No legal
   exposure, no capability; bioactivity questions stay unanswered in-house.
2. **Vendor a slice as CC BY-SA 3.0 data, segregated and attributed, after legal sign-off** — the
   slice file and its `dataset.json` carry the licence (`licence: CC-BY-SA-3.0`), the release, and
   the changes made; every tool answer carries ChEMBL attribution and the licence in its `source`;
   the server README says that redistributing the image redistributes CC BY-SA data; nothing derived
   from the slice is written into a CC0 or MIT-licensed file. Buys the capability; costs a licence
   regime the fleet has not had, and the answers to the four questions above.
3. **Vendor only a source with permissive terms instead** (e.g. a PubChem BioAssay extract, much of
   which is public-domain deposition, though depositor terms vary per assay) — avoids SA, at lower
   coverage and with its own per-depositor review.

## Decision

Proposed: **option 2, gated on legal confirmation of questions 1–4**, with option 1 as the
standing state until that confirmation exists. The licence permits the use the fleet needs if its
conditions are carried, and the conditions are mechanical (a manifest field, an attribution string,
a segregated file) once counsel has said where SA begins. Option 3 remains open as a complement, not
a substitute: it does not carry ChEMBL's curated target mapping.

## Consequences

Until counsel answers, `chembl` stays `proposed` and the backlog row stays open, re-worded to name
this record and the legal confirmation it waits on. If confirmed: `servers/chembl/` is built under
`docs/adding-a-server.md` with the attribution and licence fields above, and a fleet test pins that
any `dataset.json` whose `licence` names a ShareAlike licence carries an attribution string the
server's answers repeat.

Revisit when: counsel answers questions 1–4 in writing, or ChEMBL changes its licence for the
release that would be vendored.

## What keeps it true

Nothing yet: this is a proposal and builds nothing. On acceptance, the fleet test described under
Consequences holds it.
