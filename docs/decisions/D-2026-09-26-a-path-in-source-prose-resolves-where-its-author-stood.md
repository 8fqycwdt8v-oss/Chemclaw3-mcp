# D-2026-09-26-a-path-in-source-prose-resolves-where-its-author-stood — A path in source prose resolves where its author stood, and the elided form is spelled out

**Status:** accepted · **Date:** 2026-09-26

## What was found

Nothing checked a path cited in a first-party docstring or comment.
`tests/test_fleet.py::test_every_path_claude_md_cites_under_a_real_directory_resolves` covers
`CLAUDE.md` only, and the backlog row measured a naive extension to `packages/*/src` and
`servers/*/src` failing 52 of 86 citations: a server's source names its own `tests/` directory
server-relatively, and the fleet wrote a sibling server's module with its `src/<package>` segment
elided. Run with the rule below, the scan found thirteen citations that resolved nowhere — six
elided `servers/<name>/engine/admission.py`, a `servers/calc/tools.py` and a
`servers/rxnpredict/tools.py` that have never existed, a kit `tests/test_metrics.py` that does not
exist, a per-server `tests/test_server.py` written as though it were the kit's, calc's
`engine/pka.py` written inside `chem`, a deleted `engine/anc.py`, and chem's `engine/chem.py`
elided in calc — plus two that are paths in Chemclaw3.

## What was decided

- **A citation resolves where its author stood, most local first**: the component
  (`servers/<name>`, `packages/<name>`), its package directory, its `src/`, then the repository
  root. A token is a path when its first segment is an entry of one of those, the same self-rooting
  rule the `CLAUDE.md` check uses; a single-segment token is shorthand and is not checked.
- **The elided form is spelled out in the source, not taught to the checker.** A reader following
  `servers/calc/engine/admission.py` by hand meets the missing segment the checker would have had
  to invent, so the fix belongs in the prose. All thirteen were rewritten, none allowlisted.
- **A path in another repository is argued in `_OTHER_REPOSITORY_CITATIONS`**, keyed by file and
  token and held in both directions — the treatment `docs/BACKLOG.md` gives an
  `**Other repository:**` row.

## What keeps it true

- `tests/test_fleet.py::test_every_path_first_party_source_cites_resolves`
- `tests/test_fleet.py::test_every_other_repository_citation_is_still_cited_and_still_not_here`
