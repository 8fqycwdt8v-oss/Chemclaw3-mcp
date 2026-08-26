"""Pure computation for the `calc` server: no FastAPI, no MCP, no network, no store.

The import direction is one-way — `engine/` <- `tools.py` <- `app.py` — so the physics stays
testable with no transport installed, and a transport import can never creep into an SCF.

Reading order, because the dependency graph is deeper here than in the other servers:

- `config` · `ids` · `chem` · `solvents` · `uncertainty` · `budget` — leaves. Settings, the hash,
  the canonicalizer, the ALPB solvent table, the trust envelope, and the wall clock the in-process
  calculations check (the half `xtb_cli.run_isolated` cannot cover, since it kills a process group).
- `key` — `CalculationKey` and `Keyed`. **The duplication with Chemclaw3, documented in full
there.**
- `xtb_engine` — the tblite/RDKit boundary, and `engine_version()`, the first half of every
  `calc_version` this server emits.
- `structure` — the content-addressed geometry whose id is every xTB key's `input_hash`.
- `xtb_cli` — the optional `xtb` binary backend: installed in the shipped image, inactive there
  because `CHEMCLAW_XTB_ENGINE` is pinned to `tblite`.
- `xtb_spec` — where a version string and a `CalculationKey` are actually assembled.
- `anc` · `xtb_opt` · `xtb_hessian` · `xtb_thermo` — geometry, second derivatives, RRHO.
- `xtb` · `xtb_props` · `pka` · `solubility` · `logd` · `descriptors` — the nine tools' calculators.
- `identity` — the key of a calculation *before* it runs, which is what makes a remote cache
  lookup possible at all. It reads the same `*_inputs` and `cache_key` definitions the calculators
  do, so the two paths cannot disagree about what an answer should be stored under.
"""
