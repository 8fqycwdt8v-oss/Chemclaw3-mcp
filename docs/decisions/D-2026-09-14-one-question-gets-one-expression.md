# D-2026-09-14-one-question-gets-one-expression — One question gets one expression

**Status:** accepted · **Date:** 2026-09-14 · **Commit:** fix pass over W26/W27/W29, on top of
`f3f3c9c`. Three findings that share a shape and are small enough to record together.

## Context

Three places in the delivery and audit surface asked one question in more than one way, or answered
it with a number that was true of a different commit.

### 1. "Is there a registry" had three spellings

`Jenkinsfile` asked `params.IMAGE_REGISTRY?.trim()` in the Preflight refusal (line 75),
`!params.IMAGE_REGISTRY` in the publish branch (line 113), and `params.IMAGE_REGISTRY != ''` on the
digest report. **In Groovy a whitespace-only string is truthy while its `trim()` is not**, so
`IMAGE_REGISTRY="  "` with `DRY_RUN=false` and `RUN_GATE=false` skipped the gate refusal *and* took
the publishing branch.

Not driven — that needs a Jenkins — and in practice the push then dies on an image reference of
`"  /chemclaw-mcp-…"`. So this is an inconsistency rather than a live bypass. That is the reason to
delete it rather than the reason to keep it: the accident that saves it is a property of a registry
client's argument parsing, not of this pipeline.

### 2. The comment arguing a CI deletion stated two counts, and both were wrong

`.github/workflows/ci.yml` deleted a `manifests` job and argued the deletion with "every one of its
182 tests was already in the 2,034 the `check` job runs". Counted at both commits: `tests/` was
**182** at the parent and **207** at the commit that wrote the sentence; **2,034** matched neither
(1,903 and 2,068). `CLAUDE.md` one document over: *"a number in prose is a claim about its author's
afternoon"* — broken by the paragraph arguing a simplification.

The argument itself is sound and does not need either number: `pyproject.toml` sets
`testpaths = ["tests", "packages", "servers"]`, so `pytest -q tests` is a strict subset of the bare
run. That is a property of the configuration.

**And one behaviour was lost that the paragraph did not mention.** A job's steps are sequential, so
a ruff finding now aborts `check` before mypy or the suite runs. The deleted job was a separate
runner and reported the fleet invariants even when `check` was red at lint.

### 3. A forked package collapsed to one version in the suppressions register

`tests/test_deps_suppressions.py` read `uv.lock` into a `dict[str, str]`. `uv.lock` carries one
`package` entry per resolved version, and four packages fork at `f3f3c9c` — `numpy`,
`numpy-typing-compat`, `optype`, `scipy-stubs`. Driven, with a second `diskcache` entry written
*before* the real one: the `dict[str, str]` reader yields `5.6.3`, the argued version, and the
expiry check **passes**, while the lock resolves 5.6.3 *and* 5.7.0. None of the four forked packages
is suppressed today, so there is no live consequence — only one that arrives on somebody else's
dependency bump, silently.

## Decision

1. **The pipeline trims once.** `env.IMAGE_REGISTRY = params.IMAGE_REGISTRY?.trim() ?: ''` in
   Preflight, and every later read — the refusal, the publish branch, the registry login, the
   digest report, `IMAGE_PREFIX` — is of that. `params.IMAGE_REGISTRY` is read exactly once, where
   it is normalised, and a test asserts that count over the pipeline's **code** with comments
   stripped.
2. **The CI comment states no count of itself**, names the subset relation as the property the
   argument rests on, and says which behaviour the deletion cost. `if: ${{ !cancelled() }}` on the
   `Types` and `Tests` steps restores it inside one job: every gate still blocks, and a red one no
   longer hides the others.
3. **The lock is read as name → set of versions.** A fork then makes the expiry check *stricter* —
   the suppression's argued version must be the only one the lock resolves — and the failure names
   the others.

## What keeps it true

- `tests/test_delivery.py::test_the_pipeline_has_one_answer_to_whether_there_is_a_registry` —
  driven: restoring `!params.IMAGE_REGISTRY` in the publish branch reds it. It reads the
  comment-stripped pipeline, and that is not decoration: a first draft that matched the raw text
  passed with the normalisation commented out and `env.IMAGE_REGISTRY = params.IMAGE_REGISTRY` in
  its place — `D-2026-09-14-a-ratchet-that-matches-a-comment-holds-nothing` happening inside the
  commit that records it.
- `tests/test_delivery.py::test_a_publishing_run_cannot_skip_the_gate` — unchanged in intent,
  updated to the normalised guard.
- `tests/test_deps_suppressions.py::test_a_suppression_expires_when_its_package_moves` — driven:
  a second `diskcache` entry in `uv.lock` reds it, where the `dict[str, str]` reader passed with the
  divergent entry written first.
- The CI comment has no test and cannot have one; what replaced the two numbers is a statement
  about `testpaths`, which `pyproject.toml` holds.
