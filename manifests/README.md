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
lets a bundle here override a shipped one. That is a real capability and a real footgun, and **three
entries here use it on purpose** — `chem`, `safety` and `calc` all carry the name of a Chemclaw3
bundle. The first two are complete ports, so the override swaps one implementation for an identical
one and the order of `CHEMCLAW_CONNECTORS_DIR` is a free choice.

**`calc` is not, and it is the one to be careful with.** It carries nine of that bundle's fifteen
tools; the six that stay behind are the calibration ledger, the calculation cache and the artifact
store, plus every durable calc job. Putting this directory first therefore removes them from the
agent's surface **with no error** — so unless compute-only is what you want, Chemclaw3's own
connectors directory goes first. `docs/integration.md` has both environments and what each costs.
