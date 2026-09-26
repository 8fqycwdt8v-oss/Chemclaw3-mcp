# D-2026-09-26-a-corpus-names-who-refreshes-it-and-how-often — A corpus names who refreshes it and how often

**Status:** accepted · **Date:** 2026-09-26

## The choice

Every vendored `dataset.json` carried `retrieved_from` and a `sha256`, so what a corpus *is* could be
checked; when it was last true of its upstream could not. `MODULES.md` said "every mirrored corpus
needs a named owner and a cadence, recorded in that server's README", and at `7e3454d` no README in
the fleet carried either. `docs/BACKLOG.md` queued the smallest thing that works, and there were two
places to put it:

- **A sentence in each server's README.** What `MODULES.md` already asked for, and what nobody
  wrote: nothing reads a README, so nothing fails when it is absent.
- **Two fields in `dataset.json`, validated by the model `load_dataset` already refuses to open a
  corpus without.** Taken. A corpus with no owner or cadence now does not load, in its own server's
  suite, at its readiness probe, and in the kit's fleet-wide manifest test.

## The shape

- **`refresh_owner` is `team:<slug>` or `role:<slug>`, never a person.** A person's name goes stale
  the day they change jobs, and the corpus goes on looking owned — the same failure as a count in
  prose, with a name instead of a number. Lowercase slug, so two spellings of one team are not two
  owners.
- **`refresh_cadence` is an ISO 8601 duration in whole months or years** (`P6M`, `P12M`, `P1Y`). Not
  weeks or days: a corpus re-checked weekly is a feed, and a feed is a request-time call by another
  name; this fleet mirrors snapshots.
- A malformed value is its own error bucket (`malformed`) in `_explain`, not "blank": the key is
  there and says the wrong kind of thing, which is a different fix.
- **`Dataset` does not carry the two fields.** The dataclass is what a readiness probe and a tool's
  `citation()` read, and neither has a use for them; the four selftest "corpora" (`kinetics`,
  `suitability`, `thermalsafety`, `unitops`) are first-party constants with no upstream to refresh,
  and they construct `Dataset` directly. The fields are provenance a reviewer reads in the file,
  and the model is what holds them there.

## What was filled in, and what that is not

All eight shipped manifests now read `"refresh_owner": "team:chemclaw3-mcp-maintainers"` and
`"refresh_cadence": "P12M"`. **Both are placeholders, and this record is where that is said.** No
person or team has taken on refreshing any of these corpora; the owner is the one group that
demonstrably exists — whoever maintains this repository — and twelve months is a starting value,
not a commitment anybody has made. What the fields buy today is that the absence is now *visible
and uniform* rather than silent, and that the day a real owner is named, it is a one-line change a
test checks the shape of.

The eight corpora differ in what "refresh" means, and the uniform value hides that: the two ICH
tables are transcriptions that change when ICH publishes a revision; the reagent table (shipped
twice, byte-identical) and the hazard and genotox rules were ported from Chemclaw3 and change when
that source does; `props`' solvent sheet is hand-compiled here; and `rxnpredict`'s trust priors ship
empty and change when a calibration is run. A per-corpus cadence is the owner's call once there is
one.

## What this does not do

It does not record **when** a corpus was last checked against its upstream, so it cannot say that a
corpus is overdue. A `refreshed_on` date plus the cadence would make staleness computable — and
would make the suite fail on a calendar date with no change to the tree, which is a gate nobody
could reproduce from a commit. That trade is not taken here; if a staleness alarm is wanted it
belongs in something that runs on a schedule, not in `make check`.

## What keeps it true

- `packages/mcp_server_kit/tests/test_datasets.py::test_every_provenance_field_is_required` —
  parametrized over the model's own fields, so the two new ones are required and non-blank by the
  same test as the first six.
- `packages/mcp_server_kit/tests/test_datasets.py::test_a_refresh_owner_or_cadence_nobody_can_parse_is_refused`
  — a person's name, a free-text cadence, a zero or week duration: each refused as malformed.
- `packages/mcp_server_kit/tests/test_datasets.py::test_a_whole_month_or_year_cadence_is_accepted`
  — the positive half, so the pattern cannot tighten into refusing every shipped corpus.
- `packages/mcp_server_kit/tests/test_datasets.py::test_every_shipped_corpus_names_a_refresh_owner_and_cadence`
  and `packages/mcp_server_kit/tests/test_datasets.py::test_no_shipped_manifest_carries_a_key_the_loader_does_not_read`
  — every shipped `dataset.json`, values and key set.
