"""Pure computation for the `calc` server: no FastAPI, no MCP, no network, no store.

The import direction is one-way — `engine/` <- `tools.py` <- `app.py` — so the physics stays
testable with no transport installed, and a transport import can never creep into an SCF.

Reading order, because the dependency graph is deeper here than in the other servers:

- `config` · `ids` · `chem` · `solvents` · `uncertainty` — leaves. Settings, the hash, the
  canonicalizer, the ALPB solvent table, the trust envelope.
- `key` — `CalculationKey` and `Keyed`. **The duplication with Chemclaw3, documented in full
there.**
- `xtb_engine` — the tblite/RDKit boundary, and `engine_version()`, the first half of every
  `calc_version` this server emits.
- `structure` — the content-addressed geometry whose id is every xTB key's `input_hash`.
- `xtb_cli` — the optional `xtb` binary backend, absent from the shipped image.
- `xtb_spec` — where a version string and a `CalculationKey` are actually assembled.
- `anc` · `xtb_opt` · `xtb_hessian` · `xtb_thermo` — geometry, second derivatives, RRHO.
- `xtb` · `xtb_props` · `pka` · `solubility` · `logd` · `descriptors` — the nine tools' calculators.
"""
