# `docs/decisions/` — the record

Why this fleet is the way it is, one file per decision. [`CURRENT.md`](CURRENT.md) is the one-page
index of what is in force.

- **Name** a record `D-YYYY-MM-DD-<slug>.md`, heading `# D-YYYY-MM-DD-<slug> — Title`, start from
  [`TEMPLATE.md`](TEMPLATE.md), and add its row to the table below in date order. No numbers.
- **Write one** for a choice between options or a decline (with `Revisit when:`); a defect fix is a
  commit and a test. Records from 2026-10-07 on carry an `## Options` section.
- **Never edit** a merged record, except to add a `Superseded-by:` line; a changed decision gets a new
  record. Every record ends with `## What keeps it true`, whose test names must resolve.

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
| [D-2026-09-14-a-citation-with-no-path-check-sends-the-reader-to-the-wrong-file](D-2026-09-14-a-citation-with-no-path-check-sends-the-reader-to-the-wrong-file.md) | A citation with no path check sends the reader to the wrong file |
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
| [D-2026-09-15-a-server-with-nothing-to-load-still-has-something-to-verify](D-2026-09-15-a-server-with-nothing-to-load-still-has-something-to-verify.md) | A server with nothing to load still has something to verify |
| [D-2026-09-15-five-copies-varied-the-message-not-the-mechanism](D-2026-09-15-five-copies-varied-the-message-not-the-mechanism.md) | Five copies varied the message, not the mechanism |
| [D-2026-09-15-the-dependency-a-catalogue-proposed-was-the-one-tool-it-could-not-carry](D-2026-09-15-the-dependency-a-catalogue-proposed-was-the-one-tool-it-could-not-carry.md) | The dependency a catalogue proposed was the one tool it could not carry |
| [D-2026-09-15-the-eighth-server-arrived-with-the-shape-the-backlog-predicted](D-2026-09-15-the-eighth-server-arrived-with-the-shape-the-backlog-predicted.md) | The eighth server arrived with the shape the backlog predicted |
| [D-2026-09-15-two-formulas-for-one-quantity-make-the-convention-part-of-the-answer](D-2026-09-15-two-formulas-for-one-quantity-make-the-convention-part-of-the-answer.md) | Two formulas for one quantity make the convention part of the answer |
| [D-2026-09-16-a-bound-with-no-off-refuses-at-import-in-one-place](D-2026-09-16-a-bound-with-no-off-refuses-at-import-in-one-place.md) | A bound with no "off" refuses at import, in one place |
| [D-2026-09-16-a-default-ceiling-is-a-silent-truncation](D-2026-09-16-a-default-ceiling-is-a-silent-truncation.md) | A default ceiling is a silent truncation |
| [D-2026-09-16-a-dependency-with-no-wheel-builds-under-whatever-pip-fetches-that-day](D-2026-09-16-a-dependency-with-no-wheel-builds-under-whatever-pip-fetches-that-day.md) | A dependency with no wheel builds under whatever pip fetches that day |
| [D-2026-09-16-a-derivable-number-the-fleet-held-four-times](D-2026-09-16-a-derivable-number-the-fleet-held-four-times.md) | A derivable number the fleet held four times |
| [D-2026-09-16-a-field-the-author-wrote-is-not-a-field-that-is-missing](D-2026-09-16-a-field-the-author-wrote-is-not-a-field-that-is-missing.md) | A field the author wrote is not a field that is missing |
| [D-2026-09-16-a-hand-rolled-model-cannot-see-a-key-it-was-not-told-about](D-2026-09-16-a-hand-rolled-model-cannot-see-a-key-it-was-not-told-about.md) | A hand-rolled model cannot see a key it was not told about |
| [D-2026-09-16-a-knob-that-tunes-a-crash-guard-is-also-an-off-switch](D-2026-09-16-a-knob-that-tunes-a-crash-guard-is-also-an-off-switch.md) | A knob that tunes a crash guard is also an off switch |
| [D-2026-09-16-a-number-in-prose-is-a-claim-about-a-commit](D-2026-09-16-a-number-in-prose-is-a-claim-about-a-commit.md) | A number in prose is a claim about a commit |
| [D-2026-09-16-a-refusal-set-with-a-hole-in-it-is-not-a-refusal-set](D-2026-09-16-a-refusal-set-with-a-hole-in-it-is-not-a-refusal-set.md) | A refusal set with a hole in it is not a refusal set |
| [D-2026-09-16-a-second-belt-is-only-honest-with-something-reconciling-it](D-2026-09-16-a-second-belt-is-only-honest-with-something-reconciling-it.md) | A second belt is only honest with something reconciling it |
| [D-2026-09-16-a-server-that-holds-no-data-is-a-server-that-refuses-defaults](D-2026-09-16-a-server-that-holds-no-data-is-a-server-that-refuses-defaults.md) | A server that holds no data is a server that refuses defaults |
| [D-2026-09-16-a-stand-in-that-refuses-a-real-field-is-not-a-stand-in](D-2026-09-16-a-stand-in-that-refuses-a-real-field-is-not-a-stand-in.md) | A stand-in that refuses a real field is not a stand-in |
| [D-2026-09-16-a-transcription-is-proven-equal-not-argued-better](D-2026-09-16-a-transcription-is-proven-equal-not-argued-better.md) | A transcription is proven equal, not argued better |
| [D-2026-09-16-a-version-that-cannot-see-its-own-table-is-not-a-version](D-2026-09-16-a-version-that-cannot-see-its-own-table-is-not-a-version.md) | A version that cannot see its own table is not a version |
| [D-2026-09-16-the-driver-is-a-command-line-program-the-optimizer-is-not](D-2026-09-16-the-driver-is-a-command-line-program-the-optimizer-is-not.md) | The driver is a command-line program, the optimizer is not |
| [D-2026-09-16-the-optimizer-that-decides-the-geometry-is-not-in-the-version-string](D-2026-09-16-the-optimizer-that-decides-the-geometry-is-not-in-the-version-string.md) | The optimizer that decides the geometry is not in the version string |
| [D-2026-09-16-the-refusals-belong-in-front-of-the-library](D-2026-09-16-the-refusals-belong-in-front-of-the-library.md) | The refusals belong in front of the library |
| [D-2026-09-18-a-backend-that-writes-the-metadata-is-a-dependency-of-the-wheel](D-2026-09-18-a-backend-that-writes-the-metadata-is-a-dependency-of-the-wheel.md) | A backend that writes the metadata is a dependency of the wheel |
| [D-2026-09-18-a-ceiling-is-derived-from-the-pod-it-protects](D-2026-09-18-a-ceiling-is-derived-from-the-pod-it-protects.md) | A ceiling is derived from the pod it protects |
| [D-2026-09-18-a-corpus-that-cannot-be-read-is-a-probe-s-answer-not-an-import-error](D-2026-09-18-a-corpus-that-cannot-be-read-is-a-probe-s-answer-not-an-import-error.md) | A corpus that cannot be read is a probe's answer, not an import error |
| [D-2026-09-18-a-gate-that-does-not-read-the-tests-does-not-read-the-ratchets](D-2026-09-18-a-gate-that-does-not-read-the-tests-does-not-read-the-ratchets.md) | A gate that does not read the tests does not read the ratchets |
| [D-2026-09-18-a-gate-that-omits-a-layer-reads-like-one-that-ran-it](D-2026-09-18-a-gate-that-omits-a-layer-reads-like-one-that-ran-it.md) | A gate that omits a layer reads like one that ran it |
| [D-2026-09-18-a-narrowing-table-with-two-bases-is-two-tables](D-2026-09-18-a-narrowing-table-with-two-bases-is-two-tables.md) | A narrowing table with two bases is two tables |
| [D-2026-09-18-a-ratchet-that-observes-half-a-command-holds-half-a-gate](D-2026-09-18-a-ratchet-that-observes-half-a-command-holds-half-a-gate.md) | A ratchet that observes half a command holds half a gate |
| [D-2026-09-18-a-ratchet-that-reads-the-right-artefact-and-never-checks-what-it-does](D-2026-09-18-a-ratchet-that-reads-the-right-artefact-and-never-checks-what-it-does.md) | A ratchet that reads the right artefact and never checks what it does |
| [D-2026-09-18-a-suppression-nobody-argued-reads-as-a-reviewed-one](D-2026-09-18-a-suppression-nobody-argued-reads-as-a-reviewed-one.md) | A suppression nobody argued reads as a reviewed one |
| [D-2026-09-18-an-output-cap-is-not-a-bound-on-the-work](D-2026-09-18-an-output-cap-is-not-a-bound-on-the-work.md) | An output cap is not a bound on the work |
| [D-2026-09-18-every-py-in-the-tree-or-a-named-exemption](D-2026-09-18-every-py-in-the-tree-or-a-named-exemption.md) | Every `.py` in the tree, or a named exemption |
| [D-2026-09-18-the-last-bound-outside-the-reader-was-outside-it-by-type](D-2026-09-18-the-last-bound-outside-the-reader-was-outside-it-by-type.md) | The last bound outside the reader was outside it by type |
| [D-2026-09-19-a-bound-on-the-site-count-prices-half-the-work](D-2026-09-19-a-bound-on-the-site-count-prices-half-the-work.md) | A bound on the site count prices half the work |
| [D-2026-09-19-a-claim-about-another-repository-is-checked-by-re-reading-it](D-2026-09-19-a-claim-about-another-repository-is-checked-by-re-reading-it.md) | A claim about another repository is checked by re-reading it, and a count in prose is the same defect wherever it appears |
| [D-2026-09-19-an-atom-count-under-a-byte-cap-is-a-range-not-a-figure](D-2026-09-19-an-atom-count-under-a-byte-cap-is-a-range-not-a-figure.md) | An atom count under a byte cap is a range, not a figure, and a margin derived from a refused size is derived from nothing |
| [D-2026-09-19-the-worst-of-three-shapes-is-not-the-worst-shape](D-2026-09-19-the-worst-of-three-shapes-is-not-the-worst-shape.md) | The worst of three shapes is not the worst shape |
| [D-2026-09-26-a-cake-stays-incompressible-until-a-filtration-test-arrives](D-2026-09-26-a-cake-stays-incompressible-until-a-filtration-test-arrives.md) | A cake stays incompressible until a filtration test arrives |
| [D-2026-09-26-a-class-prior-adjusts-the-corpus-it-does-not-replace-it](D-2026-09-26-a-class-prior-adjusts-the-corpus-it-does-not-replace-it.md) | A per-class trust prior adjusts the corpus; it does not replace it |
| [D-2026-09-26-a-computed-import-is-argued-at-its-site](D-2026-09-26-a-computed-import-is-argued-at-its-site.md) | A computed import is argued at its site, not exempted with its file |
| [D-2026-09-26-a-constant-table-is-cached-where-its-compile-is-measured-to-matter](D-2026-09-26-a-constant-table-is-cached-where-its-compile-is-measured-to-matter.md) | A constant table is cached where its compile is measured to matter, and not on the strength of the pattern looking familiar |
| [D-2026-09-26-a-corpus-names-who-refreshes-it-and-how-often](D-2026-09-26-a-corpus-names-who-refreshes-it-and-how-often.md) | A corpus names who refreshes it and how often, in a shape the loader checks |
| [D-2026-09-26-a-path-in-source-prose-resolves-where-its-author-stood](D-2026-09-26-a-path-in-source-prose-resolves-where-its-author-stood.md) | A path in source prose resolves where its author stood, and the elided form is spelled out |
| [D-2026-09-26-a-pod-reports-the-bounds-it-is-running-with](D-2026-09-26-a-pod-reports-the-bounds-it-is-running-with.md) | A pod reports the bounds it is running with, on every /healthz answer |
| [D-2026-09-26-a-settings-shape-the-bound-scan-cannot-read-fails-the-suite](D-2026-09-26-a-settings-shape-the-bound-scan-cannot-read-fails-the-suite.md) | The bound scan follows inherited prefixes, nested models and aliases, and fails on what it cannot read |
| [D-2026-09-26-a-stand-in-refuses-what-its-consumer-refuses](D-2026-09-26-a-stand-in-refuses-what-its-consumer-refuses.md) | A stand-in refuses what its consumer refuses |
| [D-2026-09-26-a-stiff-dose-is-integrated-by-a-stable-scheme-not-refused](D-2026-09-26-a-stiff-dose-is-integrated-by-a-stable-scheme-not-refused.md) | A stiff semi-batch dose is integrated by an L-stable scheme, not refused |
| [D-2026-09-26-a-tool-that-runs-on-the-event-loop-cannot-be-gated](D-2026-09-26-a-tool-that-runs-on-the-event-loop-cannot-be-gated.md) | A tool that runs on the event loop cannot be gated, and the integrator gets a ceiling rather than a new scheme |
| [D-2026-09-26-a-torch-image-pins-one-thread-per-forward-pass](D-2026-09-26-a-torch-image-pins-one-thread-per-forward-pass.md) | A torch image pins one thread per forward pass |
| [D-2026-09-26-an-environment-prior-adjusts-the-table-it-does-not-replace-it](D-2026-09-26-an-environment-prior-adjusts-the-table-it-does-not-replace-it.md) | An environment trust prior adjusts the table; it does not replace it |
| [D-2026-09-26-gestis-is-not-a-source-and-the-reason-outlives-the-build](D-2026-09-26-gestis-is-not-a-source-and-the-reason-outlives-the-build.md) | GESTIS is not a source, and the reason outlives the build |
| [D-2026-09-26-one-ceiling-for-the-band-and-it-is-the-pool-not-the-probe](D-2026-09-26-one-ceiling-for-the-band-and-it-is-the-pool-not-the-probe.md) | One ceiling for chem's heavy band, derived from the pool rather than the probe |
| [D-2026-09-26-one-echo-bound-and-the-refusals-that-bypassed-it](D-2026-09-26-one-echo-bound-and-the-refusals-that-bypassed-it.md) | One echo bound, and the refusals that bypassed the four copies of it |
| [D-2026-09-26-the-consumer-s-agreement-module-is-the-trust-boundary](D-2026-09-26-the-consumer-s-agreement-module-is-the-trust-boundary.md) | The consumer's agreement module is the trust boundary |
| [D-2026-09-26-the-labeller-s-torch-is-the-lock-s-torch](D-2026-09-26-the-labeller-s-torch-is-the-lock-s-torch.md) | rxnlabel's models install from the hashed lock, and ship the lock's torch |
| [D-2026-09-27-a-cpu-pod-locks-the-cpu-torch](D-2026-09-27-a-cpu-pod-locks-the-cpu-torch.md) | The lock resolves PyTorch's CPU build on Linux, still by hash |
| [D-2026-09-27-the-cpu-torch-record-s-branch-commits-are-27aa77a](D-2026-09-27-the-cpu-torch-record-s-branch-commits-are-27aa77a.md) | The CPU-torch record's branch commits are `27aa77a` |
| [D-2026-09-27-the-fleet-runs-the-consumer-s-agreement-before-merge](D-2026-09-27-the-fleet-runs-the-consumer-s-agreement-before-merge.md) | The fleet runs the consumer's agreement suite before merge, and a deliberate lead is a label |
| [D-2026-09-30-a-full-pod-says-so-in-one-format-and-occupancy-is-the-scaling-signal](D-2026-09-30-a-full-pod-says-so-in-one-format-and-occupancy-is-the-scaling-signal.md) | A full pod says so in one fleet-wide format, and admission occupancy is the scaling signal |
| [D-2026-10-02-a-resolved-structure-is-written-without-a-dative-arrow](D-2026-10-02-a-resolved-structure-is-written-without-a-dative-arrow.md) | A resolved structure is written without a dative arrow |
| [D-2026-10-02-an-unrecognised-name-is-said-in-words](D-2026-10-02-an-unrecognised-name-is-said-in-words.md) | An unrecognised name is said in words |
| [D-2026-10-02-the-rebinding-guard-stays-on-and-is-told-the-service-name](D-2026-10-02-the-rebinding-guard-stays-on-and-is-told-the-service-name.md) | The rebinding guard stays on and is told the Service name |
| [D-2026-10-04-a-deployment-reads-its-bearer-from-the-secret-its-caller-reads](D-2026-10-04-a-deployment-reads-its-bearer-from-the-secret-its-caller-reads.md) | A Deployment reads its bearer from the Secret its caller reads |
| [D-2026-10-07-the-record-gets-lean](D-2026-10-07-the-record-gets-lean.md) | The record gets lean: decisions in ADRs, rules in CLAUDE.md, architecture in tests |
| [D-2026-10-08-the-fleet-publishes-its-contracts-as-a-pinned-git-package](D-2026-10-08-the-fleet-publishes-its-contracts-as-a-pinned-git-package.md) | The fleet owns every connector contract; Chemclaw3 takes it as a git dependency pinned to a tag |

[`Chemclaw3`]: https://github.com/8fqycwdt8v-oss/Chemclaw3
