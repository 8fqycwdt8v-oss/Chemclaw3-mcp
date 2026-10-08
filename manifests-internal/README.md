# `manifests-internal/` — the servers Chemclaw3 must **not** discover

The sibling of [`manifests/`](../manifests/), and the whole reason that directory is safe to put
on `CHEMCLAW_CONNECTORS_DIR`. One subdirectory per server here too, each a symlink to that server's
own `connector.yaml` — same rule, opposite consumer.

Chemclaw3 discovers a bundle as *any subdirectory of `connectors_dir` holding a `connector.yaml`*,
and discovery is enablement unless `CHEMCLAW_CONNECTORS_ENABLED` narrows it. So everything in
`manifests/` is a capability the agent gets. Two servers in this fleet are not that:

| Server | How Chemclaw3 actually reaches it |
| --- | --- |
| [`calc`](../servers/calc/) | From **inside** `science/calc/store.py::cached_compute`, as a backend on a cache miss. `CHEMCLAW_CALC_SERVER_URL`, `CHEMCLAW_CALC_SERVER_TOKEN_ENV` (→ `CHEMCLAW_CALC_TOKEN`). |
| [`rxnlabel`](../servers/rxnlabel/) | From a background corpus-labelling drain. `CHEMCLAW_RXNLABEL_SERVER_URL`, `CHEMCLAW_RXNLABEL_SERVER_TOKEN_ENV` (→ `CHEMCLAW_RXNLABEL_TOKEN`). |

Neither is dialled as a connector. `calc` carries a Chemclaw3 bundle's *name*, and its manifest
declares raw physics primitives with no calculation cache, no artifact store and no calibration
ledger behind them, where Chemclaw3's bundle holds `report_measurement`, `find_calculations`,
`list_artifacts`, `fetch_artifact`, `calculator_trust`, `calculator_outliers`,
`compute_thermochemistry` and the durable calc jobs. `rxnlabel` would put internal batch primitives
into the agent's prompt as tools to choose between.

This directory is what prevents both, in two layers:

1. **Nothing names it.** `manifests/` holds only connectors, and
   `tests/test_fleet_manifests.py::test_the_directory_the_export_line_names_holds_only_connectors` replicates
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
