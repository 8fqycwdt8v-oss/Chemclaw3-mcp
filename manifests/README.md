# `manifests/` — the directory Chemclaw3 points at

One subdirectory per server, each holding that server's `connector.yaml`. Chemclaw3 discovers a
bundle as "any subdirectory of `connectors_dir` containing a `connector.yaml`", and
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

**`calc` is a third entry carrying a Chemclaw3 bundle's name, and it must *not* be registered this
way.** It holds the physics behind that bundle's calculators and durable jobs — as individually
keyed primitives — and is called from inside Chemclaw3's own `cached_compute` as a backend, not
dialled as a connector. Putting this
directory on `CHEMCLAW_CONNECTORS_DIR` would let a partial port win the name and take the
calibration ledger, the calculation cache, the artifact store and every durable calc job off the
agent's surface — **with no error**. Its manifest lives here because this repository requires one
per server and `tests/test_server.py` checks it against the running surface, not because Chemclaw3
should point at it. See `docs/integration.md`.
