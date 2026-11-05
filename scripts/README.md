# `scripts/`

Operational scripts that are not part of any server's runtime.

- `offline_check.py` — runs the suite inside a network namespace with no route off the host
  (`make offline-run`): takes the network away and checks every answer is unchanged.
- `calibrate_rxnpredict_priors.py` — runs every registered forward predictor over a labelled set and
  writes the per-class trust priors `servers/rxnpredict` ranks with. An operator's script; its output
  is reviewed as a data diff with a fresh `sha256`.
- `architecture_baseline.py` — per-server cold import, `/healthz` and MCP `initialize` + `list_tools`
  latency (plus `calc`'s `calculation_key`), and the prose share of non-test source at the
  pre-programme commit and in the working tree, written to `docs/architecture-baseline.json`
  (`make architecture-baseline`).
- `bench_rpc_vs_mcp.py` — times `calc`'s `calculation_key` (the call under every cache lookup) over
  a fresh MCP session, a warm one, a pooled `POST /rpc/...` and an unpooled one, with the server in a
  thread and as its own process; prints JSON. The figures in
  `D-2026-10-14-backends-speak-typed-http-json-beside-mcp-for-one-release` come from it.
- `chembl_extract_subset.py` — builds `servers/chembl`'s data: downloads ChEMBL's SQLite release,
  refuses it unless its SHA-256 equals the digest pinned in the script and EMBL-EBI's
  `checksums.txt`, selects compounds and aggregates their activities into a small SQLite subset with
  its `dataset.json`. Run by `servers/chembl/Containerfile` for the `full` tier, and by hand to
  rebuild the committed fixture; stdlib only. Needs ~36 GB of scratch for the full tier.
- `install-chart-tools.sh` — installs the pinned, checksummed `helm` and `kubeconform` into a directory
  (the Jenkins gate runs it; a laptop can too). The pins equal `.github/workflows/ci.yml`'s, held by a
  delivery test.

Anything here that fetches data for a vendored corpus runs **outside** the serving image, and is the
one sanctioned place `MCP_EGRESS_ALLOW` may be set.

A fleet test reads this list against the directory, in both directions.
