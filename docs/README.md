# `docs/`

- [`operations.md`](operations.md) — **start here to run the fleet**: build an image, deploy a
  server to OpenShift/Kubernetes, wire it into Chemclaw3, verify it, and troubleshoot it (421, 401,
  503 readiness, session and admission refusals, egress refusals, checksum failures).
- [`integration.md`](integration.md) — wiring a Chemclaw3 checkout to this fleet, in dev and in a
  cluster, plus the failure modes worth knowing (chiefly: an unreachable connector degrades
  silently rather than erroring).
- [`adding-a-server.md`](adding-a-server.md) — the checklist for a new server.
- [`delivery.md`](delivery.md) — building and publishing the images, and where a rollout happens
  (not here: no server ships a chart).
- [`autoscaling.md`](autoscaling.md) — how each server scales, and the opt-in swap from the CPU
  HPA to KEDA on admission occupancy.
- [`decisions/`](decisions/) — why the fleet is the way it is, one file per decision, with the
  ledger and the naming convention in its own README.
- [`architecture-baseline-2026-10-07.json`](architecture-baseline-2026-10-07.json) — the
  W0.2 baseline: per-server import and handshake latency, and source prose share
  (`make architecture-baseline`).
- [`BACKLOG.md`](BACKLOG.md) — what is still open, each row naming an anchor in the tree. A queue,
  not a log: a closed row is deleted in the commit that closes it.

The conventions themselves live in [`CLAUDE.md`](../CLAUDE.md); the argument behind each lives in
`decisions/`.
