# `chemclaw_contracts`

What this fleet promises its consumer, as one installable package.

- **Every `connector.yaml`**, as package data: `manifests/<name>/` for the connectors Chemclaw3
  discovers, `manifests_internal/<name>/` for the backends (`calc`, `rxnlabel`) it must not. The files
  here are the only copies: `servers/<name>/connector.yaml` and `manifests*/<name>/connector.yaml`
  are links to them.
- **`chemclaw_contracts.calc` and `.rxnlabel`**: one request model per backend tool (`.wire()` is
  the exact argument dict) and the responses a caller reads. `servers/<name>/tests/test_contract.py`
  fails when a served schema drifts from them.
- **`contract_version(name)`** reads the `contract_version` a manifest declares; each server's
  `/healthz` reports it. Bump rules: [`docs/adding-a-server.md`](../../docs/adding-a-server.md).

```python
import chemclaw_contracts as contracts

contracts.manifests_dir()  # a directory safe to put on CHEMCLAW_CONNECTORS_DIR
contracts.manifest_path("calc")  # any server's connector.yaml
contracts.contract_version("chem")
```

The package is the only thing a consumer needs from this repository at runtime, and it needs no
network. `__version__` is the release; the consumer pins it as
`chemclaw-contracts @ git+https://github.com/8fqycwdt8v-oss/Chemclaw3-mcp@contracts-vX.Y.Z#subdirectory=packages/chemclaw_contracts`
(`D-2026-10-08-the-fleet-publishes-its-contracts-as-a-pinned-git-package`).
