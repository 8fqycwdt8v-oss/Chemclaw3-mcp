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
  steps** to converge a 76-atom molecule. A 450-atom call is therefore started and abandoned, which
  is the thing the refusal one atom above it says this server does not do. That ADR names it "a
  real defect" and sets it aside on purpose, because the ceiling is chartered on *allocation* and
  time is `xtb_inline_timeout_seconds`' to price — so it was argued and then queued nowhere.
  **What it is not is a request to lower the ceiling**: 450 is a memory bound and lowering it for a
  time reason would conflate the two. The decision is whether a relaxation gets a *cycle-count*
  bound derived from the budget and the size (which needs a converged cycle count for a large
  molecule, and nobody has one), whether the deadline's refusal should say how far it got so an
  abandoned run is legible rather than silent, or whether the honest fix is to the refusal's own
  wording. Any of the three needs one measurement first: a real relaxation at 400-450 atoms run to
  the deadline, reporting cycles completed and gradient norm, which is hours of CPU and is why this
  is a row rather than a commit.
  **Anchors:** `servers/calc/src/chemclaw_mcp_calc/engine/structure.py`,
  `servers/calc/src/chemclaw_mcp_calc/engine/budget.py`,
  `servers/calc/src/chemclaw_mcp_calc/engine/config.py`,
  `docs/decisions/D-2026-09-18-a-ceiling-is-derived-from-the-pod-it-protects.md`.

- [ ] **Neither heavy server pins its inference thread width, so a slot is a core only by
  accident.** `torch.get_num_threads()` is sized from the machine's physical cores rather than from
  the container's cgroup, and neither `servers/rxnpredict/Containerfile` nor
  `servers/rxnlabel/Containerfile` sets `OMP_NUM_THREADS` — so on a large node one forward pass in a
  two-core pod gets a thread count nobody chose. `D-2026-09-12-one-tool-call-is-not-one-thread`
  charges each call what the process is *configured* to spend, which makes an unpinned pod go serial
  rather than thrash: safe, and a smaller ceiling than the pod could support. Pinning
  `OMP_NUM_THREADS=1` the way `servers/calc/Containerfile` does would let both ceilings mean more
  than "one call at a time" — but it is a latency change to inference that **this repository has not
  measured**, because torch is an optional extra no test environment here carries. The row is
  therefore the measurement first: an image with the `models` extra, one forward pass pinned and
  unpinned, on a two-core cgroup.
  **Anchors:** `servers/rxnpredict/Containerfile`, `servers/rxnlabel/Containerfile`,
  `servers/calc/Containerfile`.

- [ ] **`enumerate_stereoisomers` is the one species tool left with no input bound.**
  `D-2026-09-26-one-ceiling-for-the-band-and-it-is-the-pool-not-the-probe` gated the band on one
  ceiling and measured what a ceiling cannot fix: four tools past a 30 s `request_timeout` or a 3 s
  `readinessProbe.timeoutSeconds` in one call on legal 1,990-atom shapes. Three are now priced
  before they run — `enumerate_tautomers` and `describe_topology`'s tautomer count by
  `MAX_TAUTOMER_HEAVY_ATOMS`, `enumerate_degradants` by `MAX_DEGRADANT_MATCH_ATOM_PRODUCT`
  (`servers/chem/tests/test_enumeration_cost_bounds.py`); re-measured on the merged engine, the
  1,991-atom polyester and polyol refuse or answer in under 0.3 s. `enumerate_stereoisomers` does
  not: the 1,991-atom polyol still costs **5.4 s** of CPU (10,226 ms in the ADR's image) before its
  output cap refuses it, the defect `D-2026-09-18-an-output-cap-is-not-a-bound-on-the-work` fixed
  for `enumerate_protonation_states`. It needs a bound priced on what drives its cost — the
  unassigned stereocentre count, not the atom count (a 996-atom PAMAM G4 is 23 ms) — measured first.
  **Anchors:** `servers/chem/src/chemclaw_mcp_chem/engine/species.py`,
  `servers/chem/src/chemclaw_mcp_chem/engine/admission.py`,
  `servers/chem/tests/test_enumeration_cost_bounds.py`.

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
  **Start from the expectation that the incumbent wins.** Over one corpus the *incumbent* SMARTS
  table led, **97.1% to 91.3%**. That run stopped before the arm that could overturn it — reactions
  where a spectator or substituent carries the diagnostic group of a different class, which is the
  false-positive mode a mapping-free table is structurally prone to and an atom-mapped classifier is
  not — so two numbers on a corpus that favoured SMARTS is evidence about that corpus and not a
  verdict. They are here because they point the opposite way from this row's framing: this may be a
  swap not worth making, and the next session should expect to find that rather than assume the
  curated table wins.
  **Anchors:** `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/meta/classifier.py::ALL_CLASSES`,
  `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/predictors/conditions/rxn_insight.py`,
  `servers/rxnlabel/src/chemclaw_mcp_rxnlabel/engine/naming.py`,
  `servers/rxnpredict/tests/test_dataset.py`.

- [ ] **`kinetics` refuses a semi-batch dose past `MAX_INTEGRATION_STEPS`, and a stable scheme
  would answer it.** `D-2026-09-26-a-tool-that-runs-on-the-event-loop-cannot-be-gated` gave
  `semibatch_accumulation_profile` an admission ceiling rather than a new integrator, because a
  gate was owed either way. What the ceiling does not change is the refusal: a dose whose
  stiffness bound times its dose time needs more than 200,000 explicit RK4 steps is turned away
  as mixing-limited. An implicit or exponentially-fitted step for the linear part would be stable at any step and would
  answer it, at the price of re-measuring the convergence order the current scheme was proven at.
  Decide whether the stiff band is worth that, and do not close it by lowering the step ceiling.
  **Anchors:** `servers/kinetics/src/chemclaw_mcp_kinetics/engine/reactors.py`,
  `servers/kinetics/src/chemclaw_mcp_kinetics/engine/admission.py`.

- [ ] **`rxnpredict`'s per-class prior override still replaces the vendored corpus whole.**
  `D-2026-09-26-an-environment-prior-adjusts-the-table-it-does-not-replace-it` made
  `CHEMCLAW_RXNPREDICT_MODEL_TRUST_PRIORS` an adjustment; `model_trust_priors_by_class` is left as
  its documented override, so one class named in `CHEMCLAW_RXNPREDICT_MODEL_TRUST_PRIORS_BY_CLASS`
  drops every other class's calibrated weights from `data/trust_priors.json`, and nothing validates
  its class labels against `classifier.ALL_CLASSES` or its predictor ids. Decide whether it merges
  onto the corpus like the global table, or stays a whole replacement that at least validates.
  **Anchors:** `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/config.py`,
  `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/meta/classifier.py`.

## 2 — Readiness, where it still stops

- [ ] **A degraded `rxnlabel` row is stamped as though it were healthy, because the stamp Chemclaw3
  writes is a deployment-level string read once per drain pass.** **Other repository:** `Chemclaw3`.
  `D-2026-09-12-a-degradation-that-is-not-counted-is-a-degradation-nobody-sees` added a third stamp
  word — `version.labeller_version(failed=...)` answering `mapper@failed` — and `tools.py` puts it on
  the *per-row* `version` field, with `degraded` beside it. Driven against a full Chemclaw3 checkout
  on 2026-09-13: neither field reaches that repository. `ingest/labels/labeller.ReactionRepresentation`
  and `ReactionNaming` are `ConfigDict(extra="ignore")` and declare neither, and
  `ingest/labels/enrich.label_stale` stamps every row with the `version` argument its planning
  activity read once from the `labeller_version` *tool* — which is `labeller_version()` with no
  `failed`, so a pod whose mapper raises on reaction 57 of 200 stamps all 200 as a healthy pod would.
  The mechanism this fleet built is correct and unread; closing it is a change over there — the two
  answer models gaining `version`, and `store_labels` stamping the row's own — and nothing here can
  check it. (A reviewer read this the other way, as rows being *permanently stale* because
  `store.stale()` accepts only `version` or `underived_stamp(version)`. That would require the
  `failed` stamp to reach the column, and it does not.)
  **Anchors (Chemclaw3):** `src/chemclaw/ingest/labels/labeller.py::ReactionRepresentation`,
  `src/chemclaw/ingest/labels/enrich.py::label_stale`,
  `src/chemclaw/science/labels/store.py::underived_stamp`.

## 3 — The gate itself

- [ ] **One install in one image still re-resolves, and it is the heaviest closure in the fleet.**
  `D-2026-09-13-an-audit-of-a-lockfile-no-image-reads-audits-nothing` put every Containerfile on
  `uv export --frozen ... --require-hashes`, and measured the result on `props`: 11 of 37 packages
  differed before, 0 after. **`rxnlabel`'s runtime stage is the exception it names**: it installs
  `"rxnmapper==0.4.3" "rxn-insight==0.1.3"` straight from PyPI through the CPU-torch
  `--extra-index-url`, so those two are version-pinned to the lock by
  `tests/test_fleet.py::test_an_image_that_installs_from_the_index_pins_what_the_audit_read` and
  their whole transitive closure — torch included — re-resolves on every build, with no hashes.
  Folding them into the export means deciding which torch build ships (the lock resolves PyPI's, the
  image deliberately takes the CPU index's), which is a measurement and an argument rather than a
  line edit. Until then this is the one image whose closure `make deps-audit` does not describe.
  **Anchors:** `servers/rxnlabel/Containerfile`, `tests/test_fleet.py`, `uv.lock`.

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

- [ ] **The consumer-side guard cannot tell the agreement module from any file with that name and
  a green test.** `test_the_consumer_still_agrees_with_the_surface_this_tree_declares` runs whatever
  module sits at the path `AGREEMENT_MODULE` names in the consumer checkout, and reads its
  outcome; `inert_outcome` now refuses a skip, an xfail, an xpass, an empty selection and a run with
  no pass in it, which closes every way that module can be *inert*. It does not close a module that
  genuinely passes and asserts nothing about this tree. Driven 2026-09-14 at `e8cf74a`: a synthetic
  checkout whose whole agreement module is one `test_*` with `assert True`, beside a symlinked
  `.venv`, gives `3 passed in 1.32s` here — the real module's three checks replaced by nothing, with
  the guard green. `D-2026-09-14-what-this-fleet-enforces-bounds-measures-and-accepts` §4.6 names
  the *deletion* case only, which is one instance of this. It is inherent to running the consumer's
  module rather than reproducing it, so the row is a decision rather than a fix: whether this side
  should also require the module to report a **minimum number of passes** it derives from the
  consumer's own file (which couples the two trees' test counts), whether it should name the tests
  it expects by `--collect-only`, or whether the honest arrangement is to say in the record that the
  consumer's file is trusted and name that as the trust boundary.
  **Anchors:** `tests/test_consumer_agreement.py::inert_outcome`,
  `tests/test_consumer_agreement.py::test_the_consumer_still_agrees_with_the_surface_this_tree_declares`,
  `docs/decisions/D-2026-09-14-what-this-fleet-enforces-bounds-measures-and-accepts.md`.

## 4 — Corpora that are not yet licensed to exist

- [ ] **ChEMBL is CC-BY-SA and `chembl` cannot be built until somebody has read what that obliges.**
  Attribution obligations follow the data into anything derived from it, which for this fleet means
  into a vendored slice, into `dataset.json`, and into whatever a tool returns as `source`. The
  question is not whether the licence permits the mirror — it does — but what the obligation is on
  a *derived* answer a chemist pastes into a report. Nothing is blocked behind it except the server
  itself, which is why it sits here rather than stopping anything.
  **Anchors:** `MODULES.md`.

- [ ] **`ghs` must be built on PubChem LCSS and ECHA C&L, and the reason has to survive the build.**
  GESTIS prohibits transfer into other information systems, so it is not a source this fleet may
  vendor at all — and a hazard corpus is exactly the kind of thing a later contributor "improves" by
  reaching for the most complete source available. The prohibition belongs in the server's
  `dataset.json` provenance and its README when it is built, not only in the catalogue.
  **Anchors:** `MODULES.md`, `docs/adding-a-server.md`.

## 5 — Consuming a server hosted elsewhere

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

## 6 — Correlations that need data nobody here has

- [ ] **`unitops` models an incompressible cake, and real organic cakes compress.** A filtration
  time from `filtration_time` takes a single specific cake resistance and assumes it is independent
  of pressure, so it overstates what pushing harder buys — and it does so in the optimistic
  direction, which is the one that gets a filter under-sized. The compressible form is
  `alpha = alpha₀·ΔPˢ`, and `s` is fitted over filtration tests at **several** pressures: a regression over
  data that exists in nobody's checkout here, and a default `s` would be this server inventing a
  compressibility. The tool's docstring says what the assumption costs and in which direction, which
  is the honest interim. What reopens it is filtration-test data arriving through an ELN — the same
  trigger `servers/kinetics`'s absent `fit_rate_law` waits on — at which point the shape is one tool
  taking `alpha₀` and `s` rather than a default anywhere.
  **Anchors:** `servers/unitops/src/chemclaw_mcp_unitops/engine/filtration.py`,
  `servers/unitops/README.md`.
