# D-2026-10-08-the-fleet-publishes-its-contracts-as-a-pinned-git-package — The fleet owns every connector contract and Chemclaw3 takes it as a git dependency pinned to a tag

**Status:** accepted · **Date:** 2026-10-08

## Context

Eight `connector.yaml` files existed twice, here and in Chemclaw3's `src/chemclaw/connectors/<name>/`
(`chem kinetics props rxnpredict safety suitability thermalsafety unitops`), 29 to 126 lines apart.
Agreement between the copies was checked only when a sibling checkout was on the machine. The `calc`
wire (tool names and argument dicts) was hard-coded in Chemclaw3's `connectors/calc/{compose,remote}.py`
and re-typed in its test fake, and nothing was versioned. The architecture programme
(`Chemclaw3`'s `D-2026-10-07-the-architecture-programme`, W2) gives each contract exactly one owner;
for connectors that owner is this repository, which runs the servers.

The artefact has to be reproducible, pinned, checkable in CI, and must need no network at runtime.
There is no package registry and this repository cannot publish a release. Chemclaw3 installs with
`uv sync --frozen --no-dev` in `deploy/Containerfile` (a build stage that has `git`); it does not
use `uv export --require-hashes`, which this fleet's own images do and which cannot hash a git
dependency. Chemclaw3 already accepts an optional, validated `contract_version` on its manifest
model (Chemclaw3 pull request 573); its model is `extra="forbid"`, so a manifest may carry the key
only once that change is on its `main`.

## Options

1. **A Python package `chemclaw-contracts`, taken by Chemclaw3 as a git dependency pinned to a tag**
   (`git+https://github.com/8fqycwdt8v-oss/Chemclaw3-mcp@contracts-vX.Y.Z#subdirectory=packages/chemclaw_contracts`).
   `uv.lock` records the resolved commit, so a moved tag cannot change a build. Needs GitHub reachable
   at *build* time, and the lock holds a commit id rather than a content hash. No release step.
2. **A wheel attached to a GitHub release, taken by direct URL and hash.** The strongest pin (the
   lock hashes the wheel; `--require-hashes` works). Needs a release pipeline with permission to
   publish assets, which this repository does not have; the wheel is built outside the tagged tree;
   release assets can be replaced.
3. **A checksummed generated snapshot vendored into Chemclaw3 with a `contracts.lock`.** No network
   at build, and the checksum is easy to check. It is also a second copy of every manifest in
   Chemclaw3 — the duplication this removes — kept honest by a regeneration step both repositories
   must remember, and it carries no Python models for the `calc` wire.

## Decision

**Option 1.** Both consumers are Python; the package is the unit of versioning; the lock pins an
immutable commit; and the one thing it adds to Chemclaw3's build, a `git clone` of a public
repository, is a network need its image build already has for wheels. Option 2 is the better pin but
cannot be operated from here; it is the upgrade path if a consumer ever needs `--require-hashes`.
Option 3 is declined because it re-creates the copies.

- **Layout.** `packages/chemclaw_contracts` is a uv workspace member. It holds every
  `connector.yaml` as package data (`manifests/` for connectors, `manifests_internal/` for the
  backends) and the typed `calc` and `rxnlabel` wire. `servers/<name>/connector.yaml` and
  `manifests*/<name>/connector.yaml` are links to those files, so the existing tests and the
  published `CHEMCLAW_CONNECTORS_DIR` line keep working. Server images build and install the package
  wheel beside `mcp-server-kit`, from the same lock.
- **Runtime.** The manifests are read from site-packages. Nothing in the package opens a socket.
- **Versions.** A manifest declares `contract_version` (bump rules in `docs/adding-a-server.md`)
  and `/healthz` reports the manifest's value, omitting it where the manifest has none. No manifest
  declares one yet: Chemclaw3 accepts the field, but its sibling agreement test compares every
  bundle-level key against its own copies of eight manifests, so the value is added once those
  copies carry it or are deleted. The package `__version__` is the release, tagged
  `contracts-vX.Y.Z` after a merge. Until a tag exists, Chemclaw3 may pin the merge commit.
- **Checking.** The agreement workflow installs *this* pull request's package into Chemclaw3's
  environment, over whatever it pins, and runs Chemclaw3's validators and sibling-agreement test;
  it fails, never skips, when Chemclaw3 is unreachable.
- **What Chemclaw3 must do** (its W2.4): add the dependency line above to `pyproject.toml`, run
  `uv lock`, delete the eight manifest-only copies keeping each bundle's `skills/`, and resolve
  manifests from `chemclaw_contracts.manifests_dir()` (put it on `CHEMCLAW_CONNECTORS_DIR`, ahead of
  its own bundles). The `calc` fake and client import `chemclaw_contracts.calc` instead of
  re-typing argument dicts.

## Consequences

A fleet change that Chemclaw3 cannot read fails here, on the pull request, instead of on Chemclaw3's
`main` afterwards. Chemclaw3 upgrades by bumping a tag in one line. The fleet gains a package it must
keep in step with the servers; `test_contract.py` in each backend fails when a served schema drifts
from the models, which is what makes that cheap. Chemclaw3's image build needs `github.com`.

**Revisit when:** Chemclaw3's image build can no longer reach `github.com` (the build fails at
`uv sync --frozen`), or a consumer needs hash-verified installs (`uv export --require-hashes` on
Chemclaw3's image); then publish option 2 from a release job and pin the wheel by URL and hash.

## What keeps it true

- `packages/chemclaw_contracts/tests/test_manifests.py::test_every_server_has_exactly_one_manifest_here`
  and `test_the_repository_paths_are_links_to_the_package_file` hold the single owner.
- `packages/chemclaw_contracts/tests/test_manifests.py::test_the_built_wheel_carries_every_manifest`
  holds that the installed package has them.
- `servers/calc/tests/test_contract.py::test_the_served_surface_is_the_contract` holds the typed wire
  against the served schemas.
- `packages/mcp_server_kit/tests/test_connector_app.py::test_healthz_reports_the_version_its_manifest_declares`
  holds that `/healthz` reports exactly the version its manifest declares, and omits it otherwise.
- `tests/test_fleet_contract_version.py::test_healthz_reports_the_manifest_s_contract_version`
  holds that for every server, and
  `test_a_declared_contract_version_is_semver_and_read_the_same_three_ways` that a present value is
  well formed. Neither requires presence while the consumer's copies lack the key.
