"""`calc` — the fast local calculators (GFN2-xTB, pKa, solubility, logD, descriptors), over MCP.

Three layers, and the import direction is one-way: `engine/` (tblite, RDKit, scipy, and the version
and key derivations) <- `tools.py` (the MCP surface the agent reads) <- `app.py` (the FastAPI
transport).

Ported from Chemclaw3's own in-tree `calc` connector. It is a **partial replacement**, and the word
*partial* is the thing to understand before wiring it up:

- **The nine request/response compute tools moved here** — `compute_xtb_energy`,
  `compute_electronic_properties`, `predict_site_reactivity`, `optimize_geometry`,
  `compute_thermochemistry`, `predict_pka`, `predict_solubility`, `predict_logd`,
  `predict_developability_profile`. Same names, same arguments, same model-facing docstrings.
- **The calculation cache, the calibration ledger, the artifact store and the durable jobs did
  not**, because they are stateful and this fleet's servers are not. Chemclaw3's `calc` bundle
  keeps `report_measurement`, `find_calculations`, `list_artifacts`, `fetch_artifact`,
  `calculator_trust` and `calculator_outliers`, plus every `jobs:` entry.

So unlike `chem` and `safety`, **this server is not a connector Chemclaw3 dials.** It keeps its
`calc` bundle and all fifteen tools, and calls this server from inside
`science/calc/store.py::cached_compute` as a backend on a cache miss. The name is still `calc`
because this repository's own rule requires the directory, the package suffix and the manifest
`name` to be one string (`tests/test_fleet.py`) — so putting this fleet's `manifests/` on
`CHEMCLAW_CONNECTORS_DIR` would let a partial port win a name collision and silently take six tools
and every durable job off the agent's surface. Read `README.md` or `docs/integration.md` before
wiring this up.

**The cache stayed behind, so its addressing travels in both directions.** Every result carries
`calc_version` and — where the source derives one — `calc_key`. But `cached_compute` takes the key
as an *argument*, so a key that arrives only on the result cannot serve the lookup: hence a tenth
tool, `calculation_key`, returning the same identity one round trip earlier and cheaply (no SCF).

That is not a convenience. The strings are assembled from `tblite`/`rdkit` distribution versions, an
`xtb --version` subprocess and seven pKa calibration settings — none of which exist on a Chemclaw3
pod after the split — and a local reconstruction would not fail loudly: `calc_version` is the
primary key of the calibration ledger (`predictions`, unique on
`(calc_type, calc_version, input_hash)`, matched exactly), and `xtb_cli.binary_version()` answers
`"absent"` rather than raising, so the string comes out well-formed, matches zero rows, and reads as
`UNCALIBRATED` rather than as an error. Silent, not loud. Because Chemclaw3 therefore never derives
a key, the only thing the two repositories must keep in step is the value of `CALCULATION_EPOCH`.
"""
