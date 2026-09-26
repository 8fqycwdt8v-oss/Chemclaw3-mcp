# BACKLOG

What is still open in this fleet, highest-consequence first. Top = next.

**1 · A queue, not a log.** A closed row is **deleted** in the commit that closes it. The commit is
the record and `git log` is the history: do not strike a row through, do not append "Done" under it,
and do not add a dated section saying a row above has gone stale. `Chemclaw3`'s own file is the
evidence for the rule rather than an argument for it, and the figure is dated because it belongs to
a checkout no test here can read: measured 2026-09-12 against a full clone of that repository, its
`docs/planning/BACKLOG.md` grew from 68 lines on the day it was created (2026-07-19) to **4,737** on
2026-08-15 — twenty-seven days — because closing a row there meant annotating it; one pass the next
day cut it to 419. That is rule 4 applied to a number instead of to a row, and the date is why it is
worth keeping: the same history read against a *shallow* clone measures a maximum of 1,534 lines and
reads as a refutation.

**2 · Every row names an anchor in the tree** — a file, a symbol, a manifest key, a port — so any
row can be checked with one `grep` instead of an argument. A row that cannot name one is not ready
to be queued. `tests/test_backlog_register.py::test_every_anchor_a_row_names_exists` opens the path
anchors; it cannot tell you whether the row is still *true*, which is a thing only a reader can do.
**Check a row against `HEAD` before working it**, and if it is wrong, correcting or deleting it is
as much a contribution as the code would have been.

**3 · No count of this file's own rows in prose.** Cite the command and let it answer:

```sh
grep -c '^- \[ \]' docs/BACKLOG.md
```

A number nobody re-derives is a claim about its author's afternoon. `CLAUDE.md` already refuses one
in three places for the same reason — the port table that published two taken ports as free is the
worked example — and `test_nowhere_in_the_file_states_a_live_row_count` keeps it out of here.

**4 · A row about another repository is marked `**Other repository:**` and names that repo's
anchor**, because no test here can open it. This repository has been burned by exactly that: the
`Ports` section published Chemclaw3's connector range as 8810-8815 while its `bo` connector sat on
8816, a number belonging to a checkout this suite cannot read. That section now records the
clearance as a dated observation rather than a boundary, and a row here gets the same treatment —
the anchor check skips it, and says out loud that it did.

Related: [`decisions/`](decisions/) — why the fleet is the way it is; a row here that turns into a
decision leaves a record behind and the row goes.

---

## 1 — The resource-bound ratchet, where it stops

- [ ] **A relaxation at `servers/calc`'s atom ceiling spends its budget instead of converging, and
  the refusal one atom above it promises the opposite.** `Structure`'s refusal says a system past
  the ceiling is "refused rather than started and abandoned";
  `D-2026-09-18-a-ceiling-is-derived-from-the-pod-it-protects` measured, on a contended four-core
  box, one optimizer cycle (two gradients) at **99.17 s for 509 atoms** and 12.21 s for 239, and
  `xtb_inline_timeout_seconds` is **780 s** — so at the 450-atom ceiling a relaxation gets on the
  order of **eleven cycles** before `budget.Deadline` stops it, against D-100's measured **177
  steps** to converge a 76-atom molecule. That ADR names it "a real defect" and sets it aside on
  purpose, because the ceiling is chartered on *allocation* and time is
  `xtb_inline_timeout_seconds`' to price. **What it is not is a request to lower the ceiling**: 450
  is a memory bound and lowering it for a time reason would conflate the two.
  **An abandoned relaxation is now legible**: `Deadline.check` takes a `progress` callable and the
  optimizer's refusal reports the gradient evaluations it completed past the input and the largest
  free gradient component it was left at, against the tolerance it had to reach
  (`test_a_relaxation_the_budget_stops_says_how_far_it_got`). What remains is the decision the
  legibility does not make: whether a relaxation gets a *cycle-count* bound derived from the budget
  and the size, or whether the honest fix is to the refusal's own wording. Both need one
  measurement nobody has: a real relaxation at 400-450 atoms run to the deadline on the shipped
  image, whose refusal now reports exactly the two figures wanted — hours of CPU, which is why this
  is a row rather than a commit.
  **Anchors:** `servers/calc/src/chemclaw_mcp_calc/engine/structure.py`,
  `servers/calc/src/chemclaw_mcp_calc/engine/budget.py`,
  `servers/calc/src/chemclaw_mcp_calc/engine/xtb_opt.py`,
  `servers/calc/src/chemclaw_mcp_calc/engine/config.py`,
  `docs/decisions/D-2026-09-18-a-ceiling-is-derived-from-the-pod-it-protects.md`.


- [ ] **The hand-written reaction classifier gates the Mixture-of-Experts priors, and the curated
  one this server already depends on is not wired to it.**
  `servers/rxnpredict`'s `engine/meta/classifier.py` is a 190-line ten-class classifier over a
  60-line `_RULES` SMARTS table; its own module docstring says to swap in Rxn-INSIGHT's classifier
  "when available", and `rxn-insight` is *already* an optional dependency of this exact server,
  already constructed in `engine/predictors/conditions/rxn_insight.py`, and already called for its
  `get_reaction_info()` dict by the sibling
  `servers/rxnlabel/src/chemclaw_mcp_rxnlabel/engine/naming.py`. That dict carries
  `CLASS` from 527 curated SMIRKS against ten hand-written rules here.
  **Two things make it a row rather than a commit.** (1) `ALL_CLASSES` is a wire contract against
  the vendored `trust_priors.json` (`servers/rxnpredict/tests/test_dataset.py` checks the priors
  against it), so adopting Rxn-INSIGHT means writing and arguing a mapping from its class vocabulary
  onto those ten keys — a new declaration, not a deletion. (2) The SMARTS path has to **stay** as
  the no-extra fallback: `rxn_insight` is behind an extra the core install does not carry, and
  `classify_reaction` is a served tool that must answer without it.
  **The environment is not a blocker and the measurement is available.** Measured 2026-09-18:
  `uv pip install "rxn-insight>=0.1.2"` resolves in this family's container and pulls the whole
  stack — `rxn-insight` 0.1.3, `rxnmapper` 0.4.3, `torch` 2.14.0, `transformers` 4.57.6, 6.0 GB —
  and `Reaction('CC(=O)O.CCO>>CC(=O)OCC.O').get_reaction_info()` answers `CLASS: Acylation`,
  `NAME: Esterification of Carboxylic Acids`, with no separate checkpoint step. So whoever works
  this row can run the comparison the change needs: a different class moves `effective_prior`,
  `consensus_score` and the candidate rank order, and that has to be measured on the probe corpus
  before anything lands.
  **The spectator arm has now been run, and on it the incumbent does not win** (2026-09-26, issue
  #128 carries the corpus, the mapped reactions and every answer). Twelve reactions built so a
  spectator or substituent carries another class's diagnostic group, plus one clean control per
  class: the SMARTS table gets **5 of 12** traps and 8 of 8 controls — it calls a Boc-Lys-OH methyl
  esterification, an aryl bromination and an ester saponification `amide_formation`, and a NaBH4
  reduction `suzuki_coupling`, because each rule only asks whether its groups are *present*.
  Rxn-INSIGHT's `NAME`, mapped onto `ALL_CLASSES` by keyword, gets **11 of 12** traps and **6 of
  8** controls — it names neither a Fischer esterification of acetic acid nor benzene nitration
  (`OtherReaction`). Rxn-INSIGHT when named, else SMARTS, gets 11 and 8. Read together with the
  earlier 97.1% to 91.3% on a clean corpus, the two classifiers fail in opposite places and neither
  is a drop-in replacement for the other; the hybrid is the candidate.
  **What that run could not do, and what closing this row needs.** It gave Rxn-INSIGHT
  **template-derived atom maps** (`keep_mapping=True`) because this machine has no torch wheel for
  `rxnmapper`, so its figures are an upper bound — production maps with `rxnmapper`, whose errors
  are exactly on crowded, multi-functional substrates like these. Three things, in order: (1) rerun
  the #128 arm and the earlier clean corpus in the `rxn_insight` extra's image with `rxnmapper`
  doing the mapping (about 20 s of CPU for twenty reactions here, without the mapper); (2) if the
  hybrid still leads, write the `NAME`-to-`ALL_CLASSES` mapping as a reviewed table beside
  `ALL_CLASSES`, since keyword matching over free-text names is itself a hand-written classifier;
  (3) measure what the changed classes do to `effective_prior`, `consensus_score` and the candidate
  rank order before anything lands. The SMARTS path stays as the no-extra fallback either way.
  **Anchors:** `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/meta/classifier.py::ALL_CLASSES`,
  `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/predictors/conditions/rxn_insight.py`,
  `servers/rxnlabel/src/chemclaw_mcp_rxnlabel/engine/naming.py`,
  `servers/rxnpredict/tests/test_dataset.py`.

- [ ] **RK4's step floor in `kinetics` lands on its stability limit, where the transient barely
  decays.** `_steps_for_stability` sets `h*lambda` to `RK4_REAL_STABILITY_LIMIT` exactly, the
  point where RK4's amplification factor is 1. When the stiffness bound is tight — first order in
  the dosed reagent and zero order in the co-reagent, so `lambda = k` for the whole dose — the
  start-up transient decays slowly across the dose and the peak comes back low: measured
  2026-09-26 at `k = 10.0005` (1 h, 40 mol into 1 volume), 2.7718e-05 against 2.7776e-05, 0.21%
  low, the dangerous direction. A margin on the limit doubles RK4's step count and so its worst
  legal call, which `engine/admission.py` sizes the ceiling on; routing floor-bound doses to the
  stable scheme (`METHOD_STABLE`, measured 0.01% *high* on a neighbouring dose at `k = 10`) costs
  about a tenth of a second but moves answers RK4 now gives. Decide which, measured.
  **Anchors:** `servers/kinetics/src/chemclaw_mcp_kinetics/engine/reactors.py::RK4_REAL_STABILITY_LIMIT`,
  `servers/kinetics/src/chemclaw_mcp_kinetics/engine/admission.py`.

## 2 — The gate itself

- [ ] **Both model images ship PyPI's CUDA torch closure to CPU-only pods.** `rxnlabel` now installs
  its `models` extra from the hashed lock export
  (`D-2026-09-26-the-labeller-s-torch-is-the-lock-s-torch`), as `rxnpredict` already did, and the
  lock resolves torch from PyPI — whose linux wheel depends on the `nvidia-*`, `cuda-*` and
  `triton` wheels. Summed from `uv.lock`'s recorded sizes for linux x86_64 / cp311, those are
  ~2.2 GB of `rxnlabel`'s ~2.9 GB closure, for a GPU no pod in this fleet has. A
  `[[tool.uv.index]]` for `download.pytorch.org/whl/cpu` with `explicit = true` and a linux-only
  `torch` source would lock `+cpu` wheels by hash instead (2.13.0+cpu cp311 manylinux x86_64 is
  published); what that costs is a re-lock for both servers and an answer to whether
  `make deps-audit`'s `pip-audit` audits a `+cpu` local version or skips it — measure the second
  before shipping the first.
  **Anchors:** `servers/rxnlabel/Containerfile`, `servers/rxnpredict/Containerfile`, `uv.lock`,
  `Makefile`.

- [ ] **The cross-repository agreement runs nowhere automated, on either side.**
  `tests/test_consumer_agreement.py` closes the direction this tree was blind in — measured
  2026-09-14, a rename of `ich_impurity_limit` carried out *completely* here (server, manifest,
  `tool-surface.json`, README, `MODULES.md` and the server's own 261 tests) left
  `servers/safety/tests` and all 212 other fleet tests green and was caught only by that file. But
  it needs a `Chemclaw3` checkout **with a built `.venv`**, and CI here clones neither, so in CI it
  skips. The consumer's side has the mirror-image problem. Decide whether one of the two CI lanes
  clones the other repository shallowly and builds it, or whether the honest arrangement is the
  skip plus the terminal notice `conftest.py::pytest_terminal_summary` now prints — which is what
  ships today.
  **Anchors:** `tests/test_consumer_agreement.py`, `conftest.py`, `.github/workflows/ci.yml`.

## 3 — Corpora that are not yet licensed to exist

- [ ] **ChEMBL is CC-BY-SA and `chembl` cannot be built until somebody has read what that obliges.**
  Attribution obligations follow the data into anything derived from it, which for this fleet means
  into a vendored slice, into `dataset.json`, and into whatever a tool returns as `source`. The
  question is not whether the licence permits the mirror — it does — but what the obligation is on
  a *derived* answer a chemist pastes into a report. Nothing is blocked behind it except the server
  itself, which is why it sits here rather than stopping anything.
  **Anchors:** `MODULES.md`.

## 4 — Consuming a server hosted elsewhere

- [ ] **`retro` cannot be consumed until six things are true of it, and this repository owes it a
  manifest.** **Other repository:** `chemclaw2_retrosynthesis` — its anchors are that repo's
  `download_weights.sh`, its `Depends(require_token)` routers, and the `fastapi-mcp` mount that may
  bypass them. The one that cannot be read off the source is the second: a bearer check applied as a
  route dependency does not cover a *mounted* MCP surface, and that must be verified against a
  running server, because it is the exact defect Chemclaw3 recorded on its own fleet (a secret
  mounted, the control recorded as enabled, every tool served to anything that could reach the pod).
  What is owed **here** is `manifests/retro/connector.yaml` with every tool classified, and a row
  saying `retrosynthesis_multi_step` is a Chemclaw3 durable job rather than a synchronous tool —
  the first entry in the catalogue that needs one.
  **Anchors:** `MODULES.md`, `manifests/README.md`.
