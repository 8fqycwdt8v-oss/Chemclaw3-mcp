# D-2026-09-26-a-pod-reports-the-bounds-it-is-running-with — A pod reports the bounds it is running with

**Status:** accepted · **Date:** 2026-09-26

## The choice

Both deployment ratchets in `tests/test_fleet.py` read the files this repository ships, through
`shipped_deployment_files`. A bound moved by a kustomize overlay applied outside this tree, a Helm
value in a deploying repository, or an operator's `kubectl set env` is invisible to them, and always
will be — the shipped files are what this suite can read. `docs/BACKLOG.md` queued the one thing
that *can* see such a change: the serving side saying what it is running with. Two ways to do that:

- **Name a handful of bounds on `/healthz` by hand** — the admission ceiling and the atom bound the
  row mentioned. Cheap, and a second list of names nothing reconciles with the first.
- **Record every bound where it is resolved, report the whole record, and hold the record to the
  derived inventory.** Taken.

This is a readiness-*payload* change, not a ratchet change, and it does not make the ratchets see
anything new. It makes the value an operator actually deployed readable from one unauthenticated
`curl`, beside the corpus versions the same route already reports.

## What changed

- `mcp_server_kit.limits` keeps a process-wide record: `report_bound(name, value)` writes it,
  `effective_bounds()` reads it sorted. `env_bound` and `env_ratio` record every value they return,
  default or override; a refused value is never recorded, because the process does not start with
  it. `report_settings(obj)` records every numeric field of a `pydantic-settings` object under the
  environment name pydantic derives (prefix plus field, upper-cased, or a literal
  `validation_alias`), skipping `bool`.
- `servers/calc/engine/config.py` and `servers/rxnpredict/engine/config.py` hand their settings
  objects to `report_settings`. The kit's own non-helper bounds record themselves where they are
  resolved: `MCP_MAX_SESSIONS` (`None` when an operator turned the ceiling off, which is exactly
  the case a shipped default cannot show), the two session timeouts, and the thread-pool width and
  headroom actually installed.
- `connector_app`'s `/healthz` carries `"bounds": {<variable>: <value>}` on **every** answer —
  ready, unready and degraded — because an unready pod is the one whose configuration an operator
  is most likely to be reading.

## What was weighed

- **`/healthz` is open.** It carries no credential, by design (a kubelet probe has no identity). The
  record is environment-variable *names* and numbers; none is a secret, and each is already in the
  shipped Deployment or the source. The same route already names the corpus versions. The
  redaction the route applies to a readiness failure is not needed here, because nothing in the
  record is free text.
- **`servers/calc`'s scientific constants are reported too.** They are numbers a deployment can
  move, and a pod computing with a moved one is exactly what an operator needs to read off a probe
  rather than off a `calc_version` mismatch in Chemclaw3's ledger later.
- **"So far" is literal.** A bound declared in a module nothing has imported yet is not in the
  record. Every server imports its tool modules before `connector_app` builds the app, and the kit's
  per-call readers record themselves during app build and lifespan, so the record is complete by
  the time a probe can be answered. `rxnpredict`'s settings are recorded when `get_settings()` first
  runs, which its `tools.py` does at import.

## What keeps it true

- `tests/test_fleet.py::test_every_bound_a_deployment_can_move_is_reported_on_the_probe` — every
  bound in the derived inventory (`numeric_env_bounds`) reaches the record: through a
  `_BOUND_HELPERS` reader, through `report_settings` in the module that declares its settings class,
  or through a literal `report_bound`.
- `tests/test_fleet.py::test_the_reporting_check_bites` — that check, failing on purpose on the
  two shapes it exists to catch.
- `packages/mcp_server_kit/tests/test_connector_app.py::test_healthz_reports_the_bounds_the_process_is_running_with`
  — an `env_bound` override, the session ceiling and the pool width, each moved by the environment
  and read back from a live `/healthz`, on the ready and the unready answer.
- `packages/mcp_server_kit/tests/test_limits.py::test_every_bound_the_two_readers_return_is_recorded`
  and `packages/mcp_server_kit/tests/test_limits.py::test_a_settings_object_is_recorded_under_the_names_its_environment_reads`
  — the record's two writers.
