# `manifests-internal/` — the servers Chemclaw3 must **not** discover

The sibling of [`manifests/`](../manifests/), and the whole reason that directory is safe to point
`CHEMCLAW_CONNECTORS_DIR` at. One subdirectory per server here too, each a symlink to that server's
own `connector.yaml` — same rule, opposite consumer.

Chemclaw3 discovers a bundle as *any subdirectory of `connectors_dir` holding a `connector.yaml`*,
and discovery is enablement unless `CHEMCLAW_CONNECTORS_ENABLED` narrows it. So everything in
`manifests/` is a capability the agent gets. Two servers in this fleet are not that:

| Server | How Chemclaw3 actually reaches it |
| --- | --- |
| [`calc`](../servers/calc/) | From **inside** `science/calc/store.py::cached_compute`, as a backend on a cache miss. `CHEMCLAW_CALC_SERVER_URL`. |
| [`rxnlabel`](../servers/rxnlabel/) | From a background corpus-labelling drain, through `rxnlabel_server_url` / `rxnlabel_server_token_env`. |

Neither is dialled as a connector, and mounting either has a consequence with no error attached to
it. `calc` carries a Chemclaw3 bundle's *name*, so first-directory-wins hands it the collision —
measured with Chemclaw3's own `_bundle_dirs()` and the published `export` line, that removes
`report_measurement`, `find_calculations`, `list_artifacts`, `fetch_artifact`, `calculator_trust`,
`calculator_outliers` and `compute_thermochemistry`, plus **all twelve** durable calc jobs, and
replaces them with 20 raw physics primitives that have no calculation cache, no artifact store and
no calibration ledger behind them. `rxnlabel` puts internal batch primitives into the agent's prompt
as tools to choose between.

**Both of those used to be prevented by prose** — in block capitals, in the same five documents that
supplied the copy-pasteable `export` that causes them. This directory is the fix, and it has two
layers:

1. **No published `export` line names it.** `manifests/` holds only connectors, and
   `tests/test_fleet.py::test_the_directory_the_export_line_names_holds_only_connectors` replicates
   Chemclaw3's discovery over it to say so.
2. **Every manifest here declares `mount: backend`, a key Chemclaw3 *refuses*.** Its
   `ConnectorManifest` is `extra="forbid"` and `registry.discovered()` loads every manifest it
   finds, so an operator who points a path here anyway gets a startup error naming the file:

   ```
   ConnectorError: .../calc/connector.yaml: invalid manifest: 1 validation error for
   ConnectorManifest / mount / Extra inputs are not permitted
   ```

   That is why a **connector's** manifest must carry no `mount:` key at all — it would abort the
   startup of the deployments that are supposed to mount it. `manifests/` is the default and is
   written nowhere; only the exception is declared.

A manifest still lives here for every server, because this repository requires one per server and
each server's `tests/test_server.py` checks it against the running surface. What changed is who may
find it.
