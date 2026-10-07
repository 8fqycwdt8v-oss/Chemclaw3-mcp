# `scripts/`

Operational scripts that are not part of any server's runtime.

- `offline_check.py` — runs the suite inside a network namespace with no route off the host
  (`make offline-run`): takes the network away and checks every answer is unchanged.
- `calibrate_rxnpredict_priors.py` — runs every registered forward predictor over a labelled set and
  writes the per-class trust priors `servers/rxnpredict` ranks with. An operator's script; its output
  is reviewed as a data diff with a fresh `sha256`.
- `architecture_baseline.py` — per-server cold import, `/healthz` and MCP `initialize` + `list_tools`
  latency (plus `calc`'s `calculation_key`), and the prose share of non-test source, written to
  `docs/architecture-baseline-<date>.json` (`make architecture-baseline`).

Anything here that fetches data for a vendored corpus runs **outside** the serving image, and is the
one sanctioned place `MCP_EGRESS_ALLOW` may be set.

A fleet test reads this list against the directory, in both directions.
