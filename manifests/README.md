# `manifests/` — the directory Chemclaw3 points at

One subdirectory per **connector**, each holding that server's `connector.yaml`. Chemclaw3 discovers
a bundle as "any subdirectory of `connectors_dir` containing a `connector.yaml`", and
`CHEMCLAW_CONNECTORS_DIR` is a `PATH`-style list, so registering this directory is one environment
variable and no code change on either side:

```sh
export CHEMCLAW_CONNECTORS_DIR="/path/to/Chemclaw3-mcp/manifests:$(python -c 'import chemclaw.connectors, pathlib; print(pathlib.Path(chemclaw.connectors.__file__).parent)')"
```

**Most deployments need only one entry from here.** Chemclaw3's image already declares `chem`,
`safety`, `rxnpredict`, `props`, `thermalsafety`, `kinetics`, `unitops` and `suitability` — the same
names and tools, pointing at these servers — so for those it needs an address and a token, not this
directory. `pyexec` is the connector it does not declare; in a cluster, mount
`pyexec/connector.yaml` alone through the Chemclaw3 chart's `extraConnectors.bundles`
([`docs/operations.md`](../docs/operations.md#3-wire-it-into-chemclaw3)).

**Every entry is a symlink to the server's own `connector.yaml`, itself a link to the file in [`packages/chemclaw_contracts`](../packages/chemclaw_contracts/), never a copy.** The manifest and
the tool surface it declares have to be edited together — a copy here would be a second declaration
of one fact, and Chemclaw3's own history is a list of second declarations that went stale while
still being believed. `servers/<name>/tests/test_server.py` checks the manifest against the tools a
running server actually advertises; that check is only meaningful if there is exactly one manifest.

Earlier directories win a name collision in Chemclaw3's discovery, so putting this directory first
makes the copy here win every name it shares with a Chemclaw3 declaration. Both copies describe the
same server, so the override changes which file is authoritative, not which code answers — with one
difference to know about: Chemclaw3's copies of `props`, `thermalsafety`, `kinetics`, `unitops` and
`suitability` declare `default_enabled: false`, and these do not, so mounting the whole directory
binds all of them on every turn unless `CHEMCLAW_CONNECTORS_ENABLED` names the set.

**Two servers must never be registered this way, and they are not in this directory.** `calc`
carries a Chemclaw3 bundle's name while holding only the physics behind it, so the override would
hand a *partial* port the collision and take the calibration ledger, the calculation cache, the
artifact store and every durable calc job off the agent's surface — **with no error**. `rxnlabel`
serves internal primitives for a background drain and has no business in a conversation's tool list.
Both are in [`../manifests-internal/`](../manifests-internal/), which no `export` line above or
anywhere else names, and both declare `mount: backend` — a key Chemclaw3's `extra="forbid"` manifest
model refuses, so an operator who points a path there anyway gets a startup error naming the file.

That split is the whole reason the command above is safe to copy, and it is held by a test rather
than by this paragraph: `tests/test_fleet_manifests.py` replicates Chemclaw3's discovery over this directory
and asserts everything it finds is a connector. See `docs/integration.md`.
