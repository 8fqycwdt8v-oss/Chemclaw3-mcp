# D-2026-09-14-a-ratchet-holds-the-set-it-enumerates — A ratchet holds the set it enumerates

**Status:** accepted · **Date:** 2026-09-14 · **Commit:** post-merge fix pass over Wave 30, on top
of `24b50ec` (PR #66).

## Context

A fresh-context review of Wave 30 drove every control the sign-off record names. The controls hold.
What did not hold is the *reach* of four of the readers around them: each enumerates a spelling — a
function-name prefix, a keyword name, two filename globs, two output substrings — and each is named
for a set strictly larger than the one it enumerates. None is a new defect; all four were measured,
with the mutation verified applied each time.

They are recorded together because the failure is one shape, and it is the shape this fleet has now
recorded three times under different subjects (`a-ratchet-measures-what-it-parses`,
`a-test-that-re-types-the-expression-under-test-asserts-nothing`,
`a-ratchet-that-matches-a-comment-holds-nothing`): **a reader stands in for the property, and the
gap between the two is invisible until somebody writes the file the reader does not see.**

### The four, measured at `24b50ec`

**1 · A suppressed test satisfies `_calls_from_collected_tests`, which its own docstring denies.**
That helper's docstring says walking every `ast.Call` was satisfied "in a test unconditionally
skipped" and that scoping to `test_*` closes it. It filters on `node.name.startswith("test_")` and
nothing else. Adding `@pytest.mark.skip` to the `props` test carrying `assert_manifest_matches`
(numstat `1 0`) left both fleet ratchets green — `14 passed` — with that server's manifest check
**and** its bearer check dead. Two clauses of the sign-off record rest on this helper. Nothing ships
a skip today; the realistic form is `@pytest.mark.skipif(not shutil.which("xtb"), …)` on `calc`,
which is invisible in CI and is what somebody adds in good faith.

**2 · `readiness=None` satisfies the readiness ratchet.** It collected keyword *names*. Deleting the
kwarg reds; `readiness=None` did not (`7 passed`) — and `mcp_server_kit/app.py`'s `if readiness is
None:` is the constant-200 branch the test exists to refuse. The realistic spelling is
`readiness=_readiness if X else None`.

**3 · Both deployment ratchets read two globs.** `SERVERS.glob("*/deploy/*.yaml")` plus
`SERVERS.glob("*/Containerfile")`, in two places. A copy of `calc`'s Deployment at
`deploy/tuning.yml` with the admission ceiling at 64 was invisible to the whole suite, and so are
`deploy/overlays/*.yaml`, `Containerfile.gpu` and `*.yaml.tpl`. The clause it holds reads "no
shipped deployment".

**4 · The consumer-agreement guard refuses a skip and not an xfail.** `" skipped" not in output and
"no tests ran" not in output`, over a run whose returncode is 0. Driven against a synthetic
consumer: a skipping module fails correctly, a renamed module fails correctly, and a
`@pytest.mark.xfail` test whose body is `assert False` **passes** — `1 xfailed`, returncode 0,
neither substring present. Low realism, real mechanism: it is the one way a consumer run can assert
nothing about this tree and be recorded as agreement.

## Decision

Each reader is widened to the set its clause names, and each widening is held by a bite test on a
synthetic subject — because in every one of the four the tree is clean, so a test over the tree
would agree with itself.

- **`_calls_from_collected_tests` skips an inert body.** `_is_inert` refuses `skip`, `skipif` and
  `xfail`, matched on the mark's bare name so `@pytest.mark.skip`, `@mark.skipif(...)` and a bare
  `@skip` read alike. `xfail` is in the set for the second reason rather than the first: such a test
  *is* collected and *does* run, so no skip count mentions it, and its body is allowed to fail. A
  `pytest.param(..., marks=…)` is deliberately not matched — that suppresses one case of a test that
  still runs for the others.
- **The readiness ratchet reads the value.** Any `None` anywhere inside the `readiness=` expression
  fails, which covers the bare literal and the conditional alike. It is still a ratchet over a
  spelling; what changes is that the one equivalent spelling meaning "no check" is no longer among
  the spellings it accepts.
- **One `shipped_deployment_files()`, recursive and suffix-blind**, used by both ratchets so they
  cannot disagree about what a deployment is. Reading a file is not understanding it, so the
  widening comes with `test_a_deployment_directory_holds_only_shapes_the_ratchets_can_read`:
  `_env_settings` dispatches on a `.yaml` suffix, so a `deployment.yml` would be parsed as a
  Containerfile, find no `ENV`, and be reported **clean** — which is worse than not reading it,
  because it then looks covered.
- **`inert_outcome` reads pytest's own counts.** At least one `passed`, and no `skipped`, `xfailed`,
  `xpassed` or `deselected` beside it. The rule is positive on purpose: a returncode of 0 is what
  pytest reports for a pass, a skip, an xfail, an xpass and an empty selection alike, so any rule
  phrased as "0 and the word X is absent" accepts three of those five.

### A correction to a merged record

`D-2026-09-14-what-this-fleet-enforces-bounds-measures-and-accepts` §4.7 says
`mypy --strict servers/*/tests` aborts with `Duplicate module named "test_no_egress"`. It aborts on
`test_admission`:

```
$ mypy --strict servers/*/tests
servers/chem/tests/test_admission.py: error: Duplicate module named "test_admission"
  (also at "servers/calc/tests/test_admission.py")
Found 1 error in 1 file (errors prevented further checking)
```

Same cause, wrong module named — and because mypy stops at the first, the named one is whichever
sorts first, which is a fact about the directory listing rather than about the duplication.

## What keeps it true

- `tests/test_fleet.py::test_a_suppressed_test_is_not_a_proof` — the bite test, on a synthetic
  module carrying an uncollected helper, a collected test, a `skip`, a `skipif` and an `xfail`.
  Driven: neutering `_is_inert` reds it while the other 115 stay green.
- `tests/test_fleet.py::test_every_server_proves_its_manifest_against_a_running_server` and
  `test_every_server_proves_its_bearer_check_against_a_running_server` — driven with each of
  `@pytest.mark.skip`, `@pytest.mark.skipif(True, …)` and `@pytest.mark.xfail` on the real `props`
  test (numstat `1 0` each): all three now red, where the shipped form passed all three.
- `tests/test_fleet.py::test_every_server_hands_connector_app_a_readiness_check` — driven with
  `readiness=None` and `readiness=_readiness if _READY else None` on `servers/props` (numstat `1 1`
  each) and with the kwarg deleted (`0 1`): all three red, where the first two passed.
- `tests/test_fleet.py::test_a_deployment_directory_holds_only_shapes_the_ratchets_can_read` — the
  parser half. Driven: a `deploy/tuning.yml` and a `deploy/extra.yml` each red it.
- `tests/test_fleet.py::test_no_shipped_deployment_widens_the_egress_allowlist` and
  `test_no_shipped_deployment_moves_a_bound_the_code_reads_from_the_environment` — the reach half,
  now through `shipped_deployment_files()`. Driven: `servers/calc/deploy/overlays/tuning.yaml`
  setting `CHEMCLAW_CALC_MAX_CONCURRENT_REQUESTS=64` reds the second, and a
  `servers/calc/Containerfile.gpu` setting `MCP_EGRESS_GUARD=off` reds the first. Both were
  invisible before, and the two globs return the same 49 files as the new reader on the tree as it
  stands — so the reach widened and nothing was added.
- `tests/test_consumer_agreement.py::test_an_inert_consumer_run_is_not_agreement` — the bite test on
  pytest's real summary lines. Driven: making `inert_outcome` return `None` unconditionally reds it.
- `tests/test_consumer_agreement.py::test_the_consumer_still_agrees_with_the_surface_this_tree_declares`
  — driven against a synthetic consumer checkout in all four arms: `xfail` now **fails** (the
  shipped assertion passed it, re-measured here), `skip` fails, a renamed module fails, and a
  genuinely passing module passes.
