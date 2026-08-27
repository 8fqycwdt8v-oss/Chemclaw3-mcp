# `manifests/` — the directory Chemclaw3 points at

One subdirectory per **connector**, each holding that server's `connector.yaml`. Chemclaw3 discovers
a bundle as "any subdirectory of `connectors_dir` containing a `connector.yaml`", and
`CHEMCLAW_CONNECTORS_DIR` is a `PATH`-style list, so registering this whole fleet is one
environment variable and no code change on either side:

```sh
export CHEMCLAW_CONNECTORS_DIR="/path/to/Chemclaw3-mcp/manifests:$(python -c 'import chemclaw.connectors, pathlib; print(pathlib.Path(chemclaw.connectors.__file__).parent)')"
```

**Every entry is a symlink to the server's own `connector.yaml`, never a copy.** The manifest and
the tool surface it declares have to be edited together — a copy here would be a second declaration
of one fact, and Chemclaw3's own history is a list of second declarations that went stale while
still being believed. `servers/<name>/tests/test_server.py` checks the manifest against the tools a
running server actually advertises; that check is only meaningful if there is exactly one manifest.

Earlier directories win a name collision in Chemclaw3's discovery, so putting this directory first
lets a bundle here override a shipped one. That is a real capability and a real footgun, and **two
entries here use it on purpose** — `chem` and `safety` are complete ports carrying their bundle's
name, so the override swaps one implementation for an identical one.

**Two servers must never be registered this way, and they are not in this directory.** `calc`
carries a Chemclaw3 bundle's name while holding only the physics behind it, so the override would
hand a *partial* port the collision and take the calibration ledger, the calculation cache, the
artifact store and every durable calc job off the agent's surface — **with no error**. `rxnlabel`
serves internal primitives for a background drain and has no business in a conversation's tool list.
Both are in [`../manifests-internal/`](../manifests-internal/), which no `export` line above or
anywhere else names, and both declare `mount: backend` — a key Chemclaw3's `extra="forbid"` manifest
model refuses, so an operator who points a path there anyway gets a startup error naming the file.

That split is the whole reason the command above is safe to copy. It used to be prevented by this
paragraph, in the same file that supplied the command — this repository's own "a README is not a
gate", applied to itself. `tests/test_fleet.py` now replicates Chemclaw3's discovery over this
directory and asserts everything it finds is a connector. See `docs/integration.md`.
