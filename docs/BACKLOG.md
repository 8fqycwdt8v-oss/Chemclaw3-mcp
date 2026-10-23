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
  the 780 s budget buys ~11 optimizer cycles against ~177 needed; decide between a cycle-count bound
  and rewording the refusal, after one real 400–450-atom run on the shipped image.
  `servers/calc/src/chemclaw_mcp_calc/engine/budget.py`, `servers/calc/src/chemclaw_mcp_calc/engine/structure.py`.

- [ ] **The hand-written reaction classifier gates the MoE priors; Rxn-INSIGHT is not wired to it.**
  Rerun issue #128's arm with `rxnmapper` mapping, write a reviewed `NAME`→`ALL_CLASSES` table, and
  measure the prior/rank shift; the SMARTS path stays as fallback.
  `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/meta/classifier.py::ALL_CLASSES`.

- [ ] **RK4's step floor in `kinetics` sits on its stability limit** (peak 0.21% low at `k = 10`):
  add a margin (doubles worst-case cost, resize the ceiling) or route floor-bound doses to the stable
  scheme. `servers/kinetics/src/chemclaw_mcp_kinetics/engine/reactors.py::RK4_REAL_STABILITY_LIMIT`.

## 2 — Corpora that are not yet licensed to exist

- [ ] **ChEMBL is CC-BY-SA; `chembl` waits on legal confirmation of what that obliges** (vendored
  slice, image distribution, an answer, a cached or noted value), laid out in
  `docs/decisions/D-2026-10-23-chembl-is-vendored-only-as-share-alike-data-after-legal-review.md`. `MODULES.md`.

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
  storm against a full pod is measured. `packages/mcp_server_kit/src/mcp_server_kit/rpc.py`.

- [ ] **A request that promises a body and never finishes it is waited on forever, on `/mcp` and
  `/rpc` alike.** Confirmed with a raw socket: `Content-Length: 900000` and one byte held the
  connection past 20 s. uvicorn's `--timeout-keep-alive` covers idle connections only, the
  Containerfiles pass no flag, and the Deployments set probes but no request timeout. Needs a read
  timeout per chunk in the body-buffering loop (answer 408), sized above the slowest legitimate
  client, with a test over a raw socket. The caller must hold the bearer and the NetworkPolicy admits
  Chemclaw3 only, hence not urgent.
  `packages/mcp_server_kit/src/mcp_server_kit/finite.py::NonFiniteLiteralRefusal`.

- [ ] **Validating a hostile batch still runs on the event loop on `/mcp`.** The message sent back
  is bounded on both transports, and `/rpc` validates a body over 64 KiB in a worker thread, but
  upstream's `Tool.run` validates `tools/call` arguments inline and formats the whole error before
  the sanitiser cuts it: a 600 KB batch of empty items is about three seconds of loop and a 100 MB
  string. Offload means wrapping each tool's `fn_metadata.call_fn_with_arg_validation`
  (a copy of six upstream lines, pinned in `test_upstream_surface.py`).
  `packages/mcp_server_kit/src/mcp_server_kit/finite.py::finite_arguments`.

## 4 — Dependencies held back from Dependabot

- [ ] **rdkit 2026.3.6 moves torsion-handle literals both repositories assert**: bump here and in
  Chemclaw3 together. `servers/chem/tests/test_torsions.py`, `pyproject.toml`.

- [ ] **`run_python` does not warn that pandas 3 made copy-on-write mandatory** (`df[m]["c"] = x`
  silently does nothing): one docstring sentence and a sandbox test.
  `servers/pyexec/src/chemclaw_mcp_pyexec/tools.py`.

## 5 — Chemclaw3 consuming the fleet chart

- [ ] **Chemclaw3's `deploy/kind/render-fleet.sh` and the tests that hold its `connectors.<name>.url`
  values against this repository read `servers/*/deploy/*.yaml`, which no longer exist.** Render the
  chart instead (`helm template`, `--set` for the kind image registry, `Never` pull policy and one
  replica), then make the core chart depend on it so a connector's URL, port and token key derive from
  `servers.<name>.port`. **Other repository:** `Chemclaw3`; the chart is
  `deploy/helm/chemclaw-fleet`, its interface `deploy/README.md`.
