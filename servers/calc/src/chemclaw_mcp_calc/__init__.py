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

So unlike `chem` and `safety`, **this manifest is not a drop-in replacement for the in-tree one, and
putting it first on `CHEMCLAW_CONNECTORS_DIR` removes six tools and every durable calc job from the
agent's surface.** The name is still `calc` because this repository's own rule requires the
directory, the package suffix, the manifest `name` and the `CHEMCLAW_CONNECTOR_URLS` key to be one
string (`tests/test_fleet.py`), so the hazard is handled by *saying so* rather than by renaming —
loudly, in `connector.yaml`, `README.md` and `docs/integration.md`. Read one of them before wiring
this up.

**What every result carries that Chemclaw3's did not: `calc_version`, and `calc_key` where the
source derives one.** The cache stayed behind but its addressing had to travel, because the strings
are assembled from `tblite`/`rdkit` distribution versions, an `xtb --version` subprocess and seven
pKa calibration settings — none of which exist on a Chemclaw3 pod after the split. That is not a
convenience: `calc_version` is the primary key of the calibration ledger (`predictions`, unique on
`(calc_type, calc_version, input_hash)`, matched exactly), and `xtb_cli.binary_version()` answers
`"absent"` rather than raising, so a client deriving it locally would produce a well-formed string
matching zero rows and read as `UNCALIBRATED` rather than as an error. Silent, not loud.
"""
