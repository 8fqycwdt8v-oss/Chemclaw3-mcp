# `manifests/` — the directory Chemclaw3 points at

One subdirectory per server, each holding that server's `connector.yaml`. Chemclaw3 discovers a
bundle as "any subdirectory of `connectors_dir` containing a `connector.yaml`", and
`CHEMCLAW_CONNECTORS_DIR` is a `PATH`-style list, so registering this whole fleet is one
environment variable and no code change on either side:

```sh
export CHEMCLAW_CONNECTORS_DIR="/path/to/Chemclaw3-mcp/manifests:$(python -c 'import chemclaw.connectors, pathlib; print(pathlib.Path(chemclaw.connectors.__file__).parent)')"
```

**An entry for a server hosted here is a symlink to that server's own `connector.yaml`, never a
copy.** The manifest and the tool surface it declares have to be edited together — a copy here would
be a second declaration of one fact, and Chemclaw3's own history is a list of second declarations
that went stale while still being believed. `servers/<name>/tests/test_server.py` checks the
manifest against the tools a running server actually advertises; that check is only meaningful if
there is exactly one manifest.

**An entry for a server hosted elsewhere is a regular file, and that is the reason this directory
exists separately from `servers/`.** `retro` is the case: a multi-container system with its own
release cadence, which Chemclaw3 reaches by address like any other connector. What such a manifest
owes the fleet is the same contract, and `tests/test_fleet.py` now holds it to it — every tool
classified exactly once, bearer auth declared with a `token_env`, a port unique across the whole
directory, and a row in `MODULES.md` recording where the server actually lives. That last one is
what separates a deliberately external manifest from an orphan left behind by a deleted server:
without it, `manifests/` would keep advertising a capability nobody can reach and nothing would
notice. These tests iterate `manifests/`, not `servers/`, for exactly that reason.

Earlier directories win a name collision in Chemclaw3's discovery, so putting this directory first
lets a bundle here override a shipped one. That is a real capability and a real footgun: a server
named `chem` here would silently replace Chemclaw3's own. Names in `MODULES.md` are chosen not to
collide.
