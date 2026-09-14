# D-2026-09-14-a-citation-with-no-path-check-sends-the-reader-to-the-wrong-file — A citation with no path check sends the reader to the wrong file

**Status:** accepted · **Date:** 2026-09-14 · **Commit:** fix pass over the adversarial review of
`811d3de`.

## Context

`docs/decisions/README.md` rests the whole record on one property: every record's
`## What keeps it true` names guards, and those names resolve.
`tests/test_decision_log.py::test_every_test_a_record_names_still_exists` is what holds it, and
`CLAUDE.md` one document over states the same rule for this repository's servers.

A record almost always writes a citation as `<path>::<test>`. The regex that reads it —

```
_TEST_CITATION = re.compile(r"`(?:[\w./*-]+::)?(test_[a-z0-9_]+)`")
```

— makes the path half **non-capturing**, and `_defined_test_names()` returned a flat `set[str]` of
every `test_*` function and module stem across the three test roots. So the path was read by
nothing. A record could cite
`test_the_pod_label_matches_the_networkpolicy_selector` behind a `tests/test_fleet.py` path — a
test that lives in all seven `servers/*/tests/test_deploy.py` and in no fleet file — and the check
would pass. (Written out as a citation here, that sentence would now fail this repository's own
suite, which is the tidiest proof of the fix available.)

This is the weaker failure of the two the record exists to prevent, and it is the one that is
*harder* to notice. A dangling name resolves to nothing and a reader knows they are lost. A name in
the wrong file resolves: the reader opens that file, does not find the guard, and concludes the
guard was removed — which is the state the citation was written to disprove.

### Measured

Every path-qualified citation in `docs/decisions/` was checked by hand against the definitions:

```
path-qualified citations: 213   mismatched: 0
```

So this closes a hole rather than papering over a mess. Nothing had to be corrected, which is also
why leaving it open was tempting and why it is worth shutting: the next one is invisible.

## Decision

`_defined_test_names()` becomes `_test_definitions() -> dict[str, set[str]]`, mapping each name to
the repository-relative files that define it, and a second test,
`test_a_record_names_the_file_its_test_lives_in`, requires a `path::test` citation's path to match
one of them.

Two things it is deliberately lenient about:

- **The path may be a glob.** `test_the_pod_label_matches_the_networkpolicy_selector` is one name in
  seven files, and `servers/*/tests/test_deploy.py` is the honest way to cite it. That is why the
  map's value is a set, and why the match is `fnmatch` rather than `Path.match` — the latter is
  inconsistent across versions about whether `*` crosses a separator.
- **A citation with no path is untouched.** A record naming `test_the_map_and_the_tree_agree` in
  prose is making a claim about a name, and the existence check is the whole of what can be asked.

Separate tests rather than one, because the two fail for different reasons and a reader fixing a
renamed test should not be handed a message about a moved file.

## What keeps it true

- `tests/test_decision_log.py::test_a_record_names_the_file_its_test_lives_in` — driven by
  re-pointing one live citation in
  `D-2026-09-14-a-total-beside-a-mutation-needs-the-invocation-that-produced-it` from
  `servers/calc/tests/test_deploy.py` to the fleet file (`git diff --numstat` → `2 2`): reds
  this test alone, naming the record, the path and the test. `test_every_test_a_record_names_still_exists`
  passed throughout that mutation, which is the hole stated as a measurement.
- `tests/test_decision_log.py::test_every_test_a_record_names_still_exists` — unchanged in
  behaviour; it now reads the same definition map.
