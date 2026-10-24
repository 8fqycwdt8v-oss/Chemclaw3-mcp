# BACKLOG

What is still open in this fleet, highest-consequence first. Top = next.

1. **A queue, not a log.** A closed row is deleted in the commit that closes it; `git log` is the
   history.
2. **Every row names an anchor in the tree** (a backticked path), so it can be checked with one
   `grep`. Check a row against `HEAD` before working it.
3. **No count of this file's rows in prose.** `grep -c '^- \[ \]' docs/BACKLOG.md` answers.
4. **A row about another repository is marked `**Other repository:**`**; the anchor check skips it
   and says so.

Related: [`decisions/`](decisions/) — a row that turns into a decision leaves a record and goes.

---

## 1 — The resource-bound ratchet, where it stops

- [ ] **A relaxation at `calc`'s atom ceiling spends its budget instead of converging.** At 450 atoms
  the 780 s budget buys ~11 optimizer cycles; a 443-atom linear polyalanine, embedded as
  `optimize_geometry` embeds it and relaxed through `optimize_structure` (tblite + geomeTRIC, one
  thread), needed 463 cycles (4,389 s on an arm64 core, unbudgeted). A cycle count barely depends on the
  host, so no budget near 780 s converges such a molecule; still owed is the time per cycle on the
  shipped image on x86_64 (under amd64 emulation it is ~900 s, not a measurement), then the choice
  between a cycle-count bound and rewording the refusal.
  `servers/calc/src/chemclaw_mcp_calc/engine/budget.py`, `servers/calc/src/chemclaw_mcp_calc/engine/structure.py`.

- [ ] **The hand-written reaction classifier answers `classify_reaction`; Rxn-INSIGHT is not wired
  to it.** On issue #128's spectator corpus with `rxnmapper`'s own maps, Rxn-INSIGHT through #128's
  keyword table names 12 of 12 traps and 8 of 8 controls, the SMARTS table 5 and 8. A prior/rank
  shift cannot be measured yet: the vendored per-class priors are empty
  (`servers/rxnpredict/src/chemclaw_mcp_rxnpredict/data/trust_priors.json`), so the class moves no
  ranking until a calibration fills them, and `aggregate_forward` classifies from reactants alone,
  where Rxn-INSIGHT (it needs a mapped product) cannot run. Owed: a reviewed `NAME`→`ALL_CLASSES`
  table, then Rxn-INSIGHT first in `classify_reaction` and `aggregate_conditions` with the SMARTS
  path as fallback, its `rxnmapper` load counted by admission.
  `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/meta/classifier.py::ALL_CLASSES`.

## 2 — Corpora that are not yet licensed to exist

## 3 — Consuming a server hosted elsewhere

- [ ] **`retro` needs a classified manifest here and a verified bearer on its mounted MCP surface.**
  **Other repository:** `chemclaw2_retrosynthesis` (`fastapi-mcp` mount, `Depends(require_token)`);
  owed here: `manifests/retro/connector.yaml`, with `retrosynthesis_multi_step` as a durable job.

## 3b — Backends on one wire

- [ ] **Remove MCP from `calc` and `rxnlabel`, and `manifests-internal/` with it.** Not before
  Chemclaw3 has run a release on `calc_transport=rpc` (its W6.3) and the contracts pin carries
  1.1.0. Then `rpc=` stays, `server.streamable_http_app()` and the session machinery go for these two,
  the manifests leave `manifests_internal/` (a backend's package description is the request models),
  and `contract_version` takes a major bump.
  `servers/calc/src/chemclaw_mcp_calc/app.py`, `servers/rxnlabel/src/chemclaw_mcp_rxnlabel/app.py`,
  `manifests-internal/`.

- [ ] **A client that gives up on a `/rpc` call is not seen by the server.** Over MCP a timed-out
  caller sent `notifications/cancelled`; over `/rpc` it closes the connection and the handler runs
  on. The tool thread outlives either (`asyncio.shield` holds the slot), so the cost is a duplicate
  on retry, as before. Worth a disconnect-driven cancel of the awaiting coroutine only if a retry
  storm against a full pod is measured, which needs `/rpc` traffic: Chemclaw3's `calc_transport`
  still defaults to `mcp`. `packages/mcp_server_kit/src/mcp_server_kit/rpc.py`.

## 4 — Dependencies held back from Dependabot

## 5 — Chemclaw3 consuming the fleet chart

- [ ] **Chemclaw3's chart does not depend on this one, so each connector's URL, port and token key is
  written twice.** Chemclaw3's kind lane and the tests that hold its `connectors.<name>.url` values
  against this repository already render this chart; what remains is the dependency (Chemclaw3's
  W6.8), deriving them from `servers.<name>.port`, and a place Helm can fetch this chart from (an OCI
  registry or a chart repository). **Other repository:** `Chemclaw3`; the chart is
  `deploy/helm/chemclaw-fleet`, its interface `deploy/README.md`.
