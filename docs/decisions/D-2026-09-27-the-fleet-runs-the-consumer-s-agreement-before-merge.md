# D-2026-09-27-the-fleet-runs-the-consumer-s-agreement-before-merge — The fleet runs the consumer's agreement suite before merge, and a deliberate lead is a label

**Status:** accepted · **Date:** 2026-09-27

## What was found

The BACKLOG row said the cross-repository agreement "runs nowhere automated, on either side". By
2026-09-27 that was only half true. `Chemclaw3`'s `.github/workflows/ci.yml` `check` job checks out
this repository at `main` (anonymous, `persist-credentials: false`, into `.sibling/Chemclaw3-mcp`),
and `make cov` runs with `CHEMCLAW_MCP_REPO` pointing at it. `tests/siblings.py` resolves that
checkout, and `tests/test_sibling_manifest_agreement.py` reads its manifests and recorded surfaces.
So the consumer checks the agreement on every one of its own runs. What it cannot do is check a
change *before* it merges here. On 2026-09-27 #129 added `chem.enumerate_substitutions` to this
fleet's `chem` manifest. Every core check went red on
that module's same-surface comparison for bundles both trees declare until core's #468 declared the
tool. This repository's `tests/test_consumer_agreement.py` would have caught it. In CI it skips,
because nothing here clones the consumer.

`Chemclaw3` is public: `git ls-remote` over anonymous HTTPS answers, and the API reports
`"private": false`. So the lane needs no token.

## What was decided

- **A separate workflow, `.github/workflows/agreement.yml`.** It runs on pull requests that touch
  what the consumer reads of this tree: `servers/*/connector.yaml`, `servers/*/tool-surface.json`,
  `manifests/**`, `manifests-internal/**`, the stand-in manifest model in
  `mcp_server_kit/testing.py`, and the test file and workflow themselves. It also runs nightly,
  because the consumer moves too, and on dispatch. It shallow-clones the consumer's `main`, builds
  that repository's environment with `uv sync --locked`, and runs `tests/test_consumer_agreement.py`
  with `CHEMCLAW3_AGREEMENT_REQUIRED=1`. A missing checkout or environment is then a failure rather
  than a skip. It is a separate file, not a job in `ci.yml`, because a path filter is a workflow
  property, and daily runs of the whole gate and the image matrix would cost far more than this
  one job. Its permissions are `contents: read`, and no `${{ }}` expression appears in a `run:`
  body. Tool *modules* are deliberately left out of the paths: the consumer reads the declared
  surface, and `tests/test_fleet.py` already fails a served tool that the manifest does not declare.
- **This side fails first, and a deliberate lead is a label.** A pull request here that adds,
  renames or reclassifies a tool on a bundle both trees declare fails the lane. The failure names
  the consumer's files to change: `src/chemclaw/connectors/<bundle>/connector.yaml`, and the
  `_ARGUED_DIVERGENCES` and declined tables in `tests/test_sibling_manifest_agreement.py`.
  Labelling the pull request `agreement:core-follows` makes that step `continue-on-error`, and a
  warning annotation records that the disagreement was let through.
  **Why a label rather than a hard block:** both sides compare against the other's `main`, and
  both require equality. A hard block here would deadlock the pair. The fleet could not merge until
  core declared the tool, and core could not merge its declaration until the fleet served it.
  Core's own `ci.yml` already prescribes the order, "a cross-repo change lands there first", so the
  fleet leads and core follows. The label turns that lead from a surprise into a statement on the
  pull request. The window in which core's `main` is red still exists, but it is announced, and the
  nightly run reports it here for as long as it lasts.

## What it does not cover

- The consumer's module is the authority on what gets compared
  (`D-2026-09-26-the-consumer-s-agreement-module-is-the-trust-boundary`). A check deleted there is
  deleted here too.
- The schema-measuring tests in the consumer's `tests/test_context_floor.py` need this fleet's
  `.venv` and remain unrun in that repository's CI, by its own argued choice.

## What keeps it true

- `tests/test_consumer_agreement.py::test_a_missing_consumer_fails_where_the_lane_requires_one`
- `tests/test_consumer_agreement.py::test_the_consumer_still_agrees_with_the_surface_this_tree_declares`
- `tests/test_consumer_agreement.py::test_an_inert_consumer_run_is_not_agreement`
- `tests/test_delivery.py::test_every_job_that_runs_the_suite_checks_out_full_history`
