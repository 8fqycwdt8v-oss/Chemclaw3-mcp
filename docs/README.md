# `docs/`

- [`integration.md`](integration.md) — wiring a Chemclaw3 checkout to this fleet, in dev and in a
  cluster, plus the failure modes worth knowing (chiefly: an unreachable connector degrades
  silently rather than erroring).
- [`adding-a-server.md`](adding-a-server.md) — the checklist for a new server.
- [`delivery.md`](delivery.md) — building and publishing the images, and where a rollout happens
  (not here: no server ships a chart).
- [`BACKLOG.md`](BACKLOG.md) — what is still open, each row naming an anchor in the tree. A queue,
  not a log: a closed row is deleted in the commit that closes it.

The conventions themselves live in [`CLAUDE.md`](../CLAUDE.md), not here. One home per rule.
