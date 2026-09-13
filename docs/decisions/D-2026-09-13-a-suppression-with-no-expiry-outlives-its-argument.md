# D-2026-09-13-a-suppression-with-no-expiry-outlives-its-argument — A suppression with no expiry outlives its argument

**Status:** accepted · **Date:** 2026-09-13 · **Commit:** wave W29, on top of `0fb12537`.
No hash is written for this pass, for the reason
`D-2026-09-12-a-bypass-that-is-not-in-the-suite-is-not-closed` §5 gives.

## Context

`make deps-audit` passes `--ignore-vuln` for eight advisories, each argued at length in the
`Makefile` against a **specific version of a specific package** — `diskcache 5.6.3`,
`setuptools 80.10.2`, `transformers 4.57.6` (five of them), `accelerate 1.14.0`.

`pip-audit` matches those flags by **id**. So a dependency that is fixed, replaced or dropped merely
stops being reported: the flag stays, the argument behind it is now about a version nobody ships,
and nothing goes red. The Makefile stated the hole itself, in the `accelerate` paragraph — "**Nothing
here goes red when a fix ships**" — and the header's instruction, "Re-derive it whenever a bump
lands", is a request to a reader rather than a control.

The list also existed only as eight flags in a `make` variable, so there was nowhere to put the
version each argument was about.

## Decision

The ids move to `[tool.chemclaw.deps-audit]` in `pyproject.toml`, one row per suppression carrying
the **package** and the **version `uv.lock` resolved when the argument was written**. The Makefile
derives `AUDIT_IGNORE` from that table, so the ids exist exactly once and the flags cannot disagree
with the declaration.

`tests/test_deps_suppressions.py` is what turns the pair into a control:

- the lock still resolves the package **to that version** — a bump brings a reader back to the
  argument *before* the new version ships, whether or not the advisory moved;
- the lock still **has** the package — a suppression for a dependency this fleet has dropped is a
  line nobody would otherwise ever delete;
- every id the Makefile argues is declared and every declared id is argued — because the flags are
  derived now, an id could be retired from the gate while its paragraph stayed, and a paragraph
  reads as a live control.

A row may carry `aliases` for the same advisory under another database's id: the Makefile quotes
`CVE-2026-9856` beside `GHSA-xrqw-3rrv-vx5w`, and without that field the third check would read one
advisory's second name as a ninth suppression nobody declared. `--ignore-vuln` is built from `id`
alone.

Deliberately **not** checked: whether the advisory is still open. That needs the network, and it is
the question `make deps-audit` itself asks.

### The prose stays in the Makefile

The argument for each suppression is the long-form reasoning beside the target that reads it, which
is where somebody debugging a red audit will be. What moved is only the machine-readable pair. The
`accelerate` paragraph is rewritten in place: the sentence describing the hole is replaced by what
now fills it, because a paragraph that still describes a known gap after the commit that closed it
is this repository's own recurring defect.

An extraction that fails yields an **empty** `AUDIT_IGNORE`, which makes the audit stricter rather
than laxer — the only direction a build-time failure may take a gate.

## What keeps it true

- `tests/test_deps_suppressions.py::test_a_suppression_expires_when_its_package_moves` — driven by
  rewriting `transformers` to `5.0.0` in the lock: five of the eight rows go red, which is the four
  `transformers` advisories plus its GHSA.
- `tests/test_deps_suppressions.py::test_a_suppression_names_a_package_the_lock_still_resolves` —
  driven by renaming `diskcache` out of the lock.
- `tests/test_deps_suppressions.py::test_the_makefile_argues_exactly_the_suppressions_that_are_declared`
  — driven by deleting one row from the table while its paragraph stayed.
