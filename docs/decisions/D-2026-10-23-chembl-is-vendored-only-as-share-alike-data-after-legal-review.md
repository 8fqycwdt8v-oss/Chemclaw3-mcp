# D-2026-10-23-chembl-is-vendored-only-as-share-alike-data-after-legal-review — ChEMBL is vendored only as share-alike data, after legal review

**Status:** accepted · **Date:** 2026-10-24 · **Answers:** the `docs/BACKLOG.md` row *"ChEMBL is
CC-BY-SA; `chembl` waits on what that obliges for a derived answer"*

**Decided by the owner 2026-10-24: option 2, and the server is built.** Legal review is **cleared by
the owner's statement of 2026-10-24; not verified by the author of this record.** This record is not
legal advice and settles no legal question: it states what the licence text says, what the build does
to carry its conditions, and what it leaves for the reviewer the owner has already consulted. The
proposal this record began as (2026-10-23) was gated on that review; the gate is now the owner's word,
recorded here, rather than a document in this tree.

## Context

`chembl` (port 8862) was `proposed` in `MODULES.md` — compound lookup and bioactivities over a
vendored ChEMBL slice — and waited on one question: what the ChEMBL licence obliges once its data
sits in this repository, in a server image, and in an answer the agent quotes. No `servers/chembl/`
existed, no `dataset.json` named ChEMBL, and every vendored corpus was CC0 first-party content, so
this is the first third-party copyleft corpus in the fleet, beside MIT-licensed code (`LICENSE`).

What the licence states, as published by EMBL-EBI with release 37 (`LICENSE` and
`REQUIRED.ATTRIBUTION` in the release directory):

- ChEMBL is distributed under **Creative Commons Attribution-ShareAlike 3.0 Unported**
  (CC BY-SA 3.0). Under the BY clause EMBL-EBI requests attribution, cites the 2019 database paper
  (Mendez et al., *Nucleic Acids Res.* 47(D1):D930–D940), and asks that ChEMBL ids are preserved and
  the release number is clearly displayed when ChEMBL is incorporated into other works.
- **Attribution (BY):** whoever distributes the work or an adaptation keeps the credit, names the
  licence or links to it, and says that changes were made.
- **ShareAlike (SA):** an *Adaptation* that is distributed must be under the same or a compatible
  licence. SA is triggered by distribution, not by internal use, and attaches to the database and to
  adaptations of it, not to the MIT-licensed code beside it.
- 3.0 Unported does not license the EU *sui generis* database right as 4.0 does, so in the EU the
  position on extracting a substantial part of the database rests on the licence's general grant and
  on national law. Individual facts (an IC50, a target name) are generally not copyrightable as such;
  the selection and arrangement of the database, and in the EU the database right, are what is
  licensed.

## Options

1. **Decline `chembl`, as GESTIS was declined** — no copyleft corpus in the fleet. No legal
   exposure, no capability; bioactivity questions stay unanswered in-house.
2. **Vendor a subset as CC BY-SA 3.0 data, segregated and attributed, after legal sign-off** — the
   subset file and its `dataset.json` carry the licence, the release and the changes made; every
   tool answer carries ChEMBL attribution and the licence as a structured field; the server README
   says that redistributing the image redistributes CC BY-SA data; nothing derived from the subset is
   written into a CC0 or MIT-licensed file. Buys the capability; costs a licence regime the fleet
   has not had.
3. **Vendor only a source with permissive terms instead** (e.g. a PubChem BioAssay extract, much of
   which is public-domain deposition, though depositor terms vary per assay) — avoids SA, at lower
   coverage and with its own per-depositor review, and without ChEMBL's curated target mapping.

## Decision

**Option 2.** The legal gate the proposal named is cleared by the owner's statement; the build
carries the licence's conditions mechanically, which is what the options said it would take.

What was built, and the choices inside option 2 the owner left to the build:

- **A subset of the current release, ChEMBL 37**, pinned by release number and by the SHA-256 of the
  archive it is extracted from. Compound structures, names and synonyms, and bioactivity
  *summaries* — per compound, target, standard type and unit: counts, median, minimum, maximum,
  median pChEMBL, assay-confidence range. Never ChEMBL's per-measurement records, so the subset is a
  selection and an aggregation (the changes the licence asks to be stated), not a copy.
- **The data is produced by `scripts/chembl_extract_subset.py`**, which downloads the release,
  refuses it unless its digest equals both the pin in the script and EMBL-EBI's `checksums.txt`, and
  is deterministic. The full subset is a **build-time artifact of the image**
  (`servers/chembl/Containerfile`), not committed: this repository vendors corpora measured in
  kilobytes and has no large-file mechanism, and a subset of the size the owner scoped does not fit
  that. A small **fixture** subset built by the same script from named compounds *is* committed and is
  what the suite, a source checkout and a CI image run against. The tier (`full` or `fixture`) is
  written into the file, its `dataset.json` version and every answer. The image defaults to the
  fixture and the release pipeline (`Jenkinsfile`) asks for `full`: a default of `full` would make
  every pull request's image job download 5.8 GB and write 30 GB.
- **The notice is a field, not prose.** Every answer carries `attribution` (source and release,
  licence and licence URL, credit, a statement of changes, the share-alike condition, the citation,
  the data tier), a `source` line and a `caveat`. A fleet test holds that any `dataset.json` naming a
  share-alike licence belongs to a server whose README carries the database-right caveat; the
  server's own test holds the field for each tool.
- **The sui generis database-right caveat is documentation, not code**: the server's README states
  it, so that whoever redistributes the image reads it where they meet the image.
- **No request-time access to EMBL-EBI.** The only network contact is the image build's `subset`
  stage; the runtime image holds the subset, the egress guard is armed and the NetworkPolicy denies
  egress as for every server.

## Consequences

`servers/chembl/` exists under `docs/adding-a-server.md`; `MODULES.md` lists it as built; its
manifest is `default_enabled: false`, so it reaches an agent only where a deployment names it (a
Chemclaw3 decision, not this repository's). A fleet image or this repository handed to a party
outside the owner's legal entity distributes CC BY-SA data: that is the case the README warns
about and the one the owner's review addressed. An answer the agent stores long-term (a result cache,
knowledge notes) can accumulate ChEMBL values; the attribution travels with each answer, and whether
an accumulation approaches a substantial part of the database is the open question this record does
not close.

Refreshing is a reviewed edit of `RELEASE` and `SQLITE_SHA256` in the script, never a follow of
`latest/`; `dataset.json` names `team:chemclaw3-mcp-maintainers` and a cadence.

**Revisit when:** the owner withdraws the clearance, or ChEMBL changes its licence for a release
that would be vendored (`tests/test_fleet_data.py::test_a_share_alike_corpus_is_attributed_and_the_licence_is_named`
reads the manifest of whichever release is vendored).

## What keeps it true

- `tests/test_fleet_data.py::test_a_share_alike_corpus_is_attributed_and_the_licence_is_named` —
  every share-alike `dataset.json` names its licence and release, and its server's README carries the
  database-right caveat.
- `servers/chembl/tests/test_attribution.py::test_every_tool_answer_carries_the_attribution` — both
  tools, a hit and a miss, carry the structured notice.
- `servers/chembl/tests/test_dataset.py::test_the_subset_is_the_file_the_manifest_approved` — the
  subset's checksum, schema version and release agree with its manifest.
- `servers/chembl/tests/test_extract.py::test_the_archive_is_refused_unless_both_digests_agree` —
  the script refuses an archive that is not the pinned one.
- `tests/test_delivery.py::test_a_release_build_of_chembl_asks_for_the_full_subset` — the default is
  the fixture, and the pipeline that publishes images passes `CHEMBL_TIER=full`.
