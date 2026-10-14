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

Anything here that fetches data for a vendored corpus runs **outside** the serving image, and is the
one sanctioned place `MCP_EGRESS_ALLOW` may be set.

A fleet test reads this list against the directory, in both directions.
