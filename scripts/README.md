# `scripts/`

Operational scripts that are not part of any server's runtime.

- `offline_check.py` — runs the suite inside a network namespace with no route off the host
  (`make offline-run`). The strongest form of the no-egress claim, because it does not trust this
  repository's own code: it takes the network away and checks every answer is unchanged.

Anything here that fetches data for a vendored corpus runs **outside** the serving image, and is the
one sanctioned place `MCP_EGRESS_ALLOW` may be set.
