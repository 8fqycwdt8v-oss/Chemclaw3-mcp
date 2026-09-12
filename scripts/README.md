# `scripts/`

Operational scripts that are not part of any server's runtime.

- `offline_check.py` — runs the suite inside a network namespace with no route off the host
  (`make offline-run`). The strongest form of the no-egress claim, because it does not trust this
  repository's own code: it takes the network away and checks every answer is unchanged.

- `calibrate_rxnpredict_priors.py` — runs every registered forward predictor over a labelled set and
  writes the per-class trust priors `servers/rxnpredict` ranks with. An operator's script: it needs
  every predictor installed, takes as long as inference does, and its output is reviewed as a data
  diff with a fresh `sha256`.

Anything here that fetches data for a vendored corpus runs **outside** the serving image, and is the
one sanctioned place `MCP_EGRESS_ALLOW` may be set.

`tests/test_fleet.py::test_the_scripts_map_lists_everything_beside_it` reads this list against the
directory, in both directions — this file listed one of the two scripts here for a month.
