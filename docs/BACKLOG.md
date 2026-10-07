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

- [ ] **ChEMBL is CC-BY-SA; `chembl` waits on what that obliges for a derived answer** (vendored
  slice, `dataset.json`, a tool's `source`). `MODULES.md`.

## 3 — Consuming a server hosted elsewhere

- [ ] **`retro` needs a classified manifest here and a verified bearer on its mounted MCP surface.**
  **Other repository:** `chemclaw2_retrosynthesis` (`fastapi-mcp` mount, `Depends(require_token)`);
  owed here: `manifests/retro/connector.yaml`, with `retrosynthesis_multi_step` as a durable job.

## 4 — Dependencies held back from Dependabot

- [ ] **rdkit 2026.3.6 moves torsion-handle literals both repositories assert**: bump here and in
  Chemclaw3 together. `servers/chem/tests/test_torsions.py`, `pyproject.toml`.

- [ ] **`run_python` does not warn that pandas 3 made copy-on-write mandatory** (`df[m]["c"] = x`
  silently does nothing): one docstring sentence and a sandbox test.
  `servers/pyexec/src/chemclaw_mcp_pyexec/tools.py`.
