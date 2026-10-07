"""Pure computation for the `calc` server: no FastAPI, no MCP, no network, no store.

Reading order:

- `config` · `ids` · `chem` · `solvents` · `uncertainty` · `budget` — leaves: settings, the hash,
  the canonicaliser, the ALPB solvent table, the trust envelope, the in-process wall clock.
- `key` — `CalculationKey` and `Keyed`.
- `xtb_engine` — the tblite/RDKit boundary and `engine_version()`.
- `structure` — the content-addressed geometry whose id is every xTB key's `input_hash`.
- `xtb_cli` — the optional `xtb` binary backend (shipped, inactive: the engine is pinned to tblite).
- `xtb_spec` — where version strings and keys are assembled.
- `xtb_opt` · `xtb_hessian` · `xtb_thermo` — geometry, second derivatives, RRHO.
- `xtb` · `xtb_props` · `pka` · `solubility` · `logd` · `descriptors` — the tools' calculators.
- `identity` — a calculation's key before it runs, from the same definitions the calculators use.
"""
