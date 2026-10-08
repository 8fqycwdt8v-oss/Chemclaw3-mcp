# `manifests/` — the connectors Chemclaw3 discovers

One subdirectory per **connector**, each holding that server's `connector.yaml`. Chemclaw3 discovers
a bundle as "any subdirectory of `connectors_dir` containing a `connector.yaml`".

**A deployment mounts none of this.** Every connector's manifest reaches Chemclaw3's image as package
data of the pinned `chemclaw-contracts`, and Chemclaw3 refuses a connector name found in two
directories, so putting this directory on `CHEMCLAW_CONNECTORS_DIR` beside the installed package is
a startup error. A connector needs an address and a token only
([`docs/operations.md`](../docs/operations.md#3-wire-it-into-chemclaw3)).

**Every entry is a symlink to the server's own `connector.yaml`, itself a link to the file in [`packages/chemclaw_contracts`](../packages/chemclaw_contracts/), never a copy.** The manifest and
the tool surface it declares have to be edited together — a copy here would be a second declaration
of one fact, and the package is the only place Chemclaw3 reads it from. `servers/<name>/tests/test_server.py` checks the manifest against the tools a
running server actually advertises; that check is only meaningful if there is exactly one manifest.

`props`, `thermalsafety`, `kinetics`, `unitops`, `suitability` and `pyexec` declare
`default_enabled: false`, so an empty `CHEMCLAW_CONNECTORS_ENABLED` binds none of them.

**Two servers must never be registered this way, and they are not in this directory.** `calc`
carries a Chemclaw3 bundle's name while holding only the physics behind it, so registering it as a
connector would put a *partial* port in place of the calibration ledger, the calculation cache, the
artifact store and every durable calc job. `rxnlabel` serves internal primitives for a background
drain and has no business in a conversation's tool list. Both are in
[`../manifests-internal/`](../manifests-internal/), which nothing names, and both declare
`mount: backend` — a key Chemclaw3's `extra="forbid"` manifest model refuses, so an operator who
points a path there anyway gets a startup error naming the file.

That split is held by a test rather than by this paragraph: `tests/test_fleet_manifests.py`
replicates Chemclaw3's discovery over this directory and asserts everything it finds is a
connector. See `docs/integration.md`.
