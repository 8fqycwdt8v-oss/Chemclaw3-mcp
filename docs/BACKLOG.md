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

## 1 — The no-egress posture, where it stops

- [ ] **A dynamic import whose name is computed from a *value* is outside the static scan, and
  always will be.** `importlib.import_module("gr" + "pc")` is folded to `grpc` since 2026-09-12, but
  `import_module(name)` cannot be resolved by any static reader, and `servers/rxnpredict` loads its
  optional predictor plug-ins exactly that way — so flagging the shape would fail correct code and
  teach the next reader to reach for `exempt`. The same is true of an address assembled at runtime.
  What covers them is the runtime guard for anything going through Python and `make offline-run` for
  anything that is not, which is the target `make check` does not run — the row above. Decide
  whether that pair is the answer or whether a server loading plug-ins owes a manifest of the module
  names it may load, which *is* statically checkable.
  **Anchors:** `packages/mcp_server_kit/src/mcp_server_kit/no_egress.py`,
  `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/predictors`.

- [ ] **A path cited in a module docstring is checked by nothing, and the check that would do it is
  not the one `CLAUDE.md` gets.** Until 2026-09-12
  `packages/mcp_server_kit/src/mcp_server_kit/no_egress.py` named the pyexec sandbox at a path
  missing its `src/<package>` segment, in two places, both copied rather than opened — the failure
  `test_every_path_claude_md_cites_under_a_real_directory_resolves` exists to stop, one document
  over. Extending that test to first-party source prose was measured the same day and is **not** a
  one-liner: of 86 rooted path tokens under `packages/*/src` and `servers/*/src`, 52 do not resolve
  from the repository root — a server's docstrings name their own tests directory *server-relatively*
  and the fleet writes a sibling server's engine module with the `src/<package>` segment elided. So
  the row is the resolution rule rather than the glob: decide whether a citation inside a server's
  own source resolves against that server first, and whether the elided form is spelled out or
  taught to the checker.
  **Anchors:** `tests/test_fleet.py::test_every_path_claude_md_cites_under_a_real_directory_resolves`,
  `packages/mcp_server_kit/src/mcp_server_kit/no_egress.py`,
  `servers/calc/src/chemclaw_mcp_calc/engine/admission.py`.

## 2 — The resource-bound ratchet, where it stops

- [ ] **Neither ratchet can see a pod `env:` a cluster operator adds outside these files.** Both
  read the tree, through `tests/test_fleet.py::shipped_deployment_files` — every file under
  `servers/*/deploy/` and every `servers/*/Containerfile*`. (That sentence used to name two globs,
  `servers/*/deploy/*.yaml` and `servers/*/Containerfile`, and it was describing a second hole
  rather than this one: a `deploy/tuning.yml` or an overlay directory was invisible to both
  ratchets. `D-2026-09-14-a-ratchet-holds-the-set-it-enumerates` closed that; what stays open is
  the row's actual subject.) A bound moved by a kustomize overlay applied outside this tree, a Helm
  value in a deploying repository, or an operator's `kubectl set env` is invisible here and always
  will be — the shipped files are what this suite can read. What is not yet decided is whether the
  serving side should *say* what it is running: an admission ceiling and an atom
  bound reported on `/healthz` beside the corpus versions would make the live value observable from
  a probe rather than inferred from an image. That is a readiness-payload change, not a ratchet
  change. (An `envFrom` block, a `valueFrom:` reference and a `command:` assignment are all refused
  outright now. This sentence used to say `envFrom` was "the one case a file can hide", which was
  false when it was written: the other two were parsed as setting nothing at all.)
  **Anchors:** `tests/test_fleet.py::_bound_offences`, `servers/calc/deploy/deployment.yaml`,
  `packages/mcp_server_kit/src/mcp_server_kit/app.py`.

- [ ] **The derived bound set covers scalar settings fields only, and one real container-typed field
  is env-settable.** `_numeric_settings_fields` takes `int`/`float` annotations (and `X | None`),
  deliberately: a number inside `dict[str, float]` is not a bound the ratchet could compare. But
  `servers/rxnpredict`'s `model_trust_priors` is a `dict[str, float]` with a `mode="before"`
  validator that parses a JSON string, under `env_prefix="CHEMCLAW_RXNPREDICT_"` — so it is
  environment-settable, and measured on 2026-09-12 the env value **replaces the whole table** rather
  than merging into it: `CHEMCLAW_RXNPREDICT_MODEL_TRUST_PRIORS='{"parrot": 9.9}'` leaves the
  aggregator with one prior and every other predictor unweighted. That is a scientific behaviour
  change by environment variable, which is the class the calc constants are protected as. Decide
  whether the ratchet covers container annotations whose validator accepts a string, or whether this
  field is argued in the register instead.
  **Anchors:** `tests/test_fleet.py::_numeric_settings_fields`,
  `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/config.py`.

- [ ] **The bound derivation reads two configuration mechanisms and three shapes past them are
  invisible, one of them under the wrong name.** Measured 2026-09-12 against synthetic modules, none
  of these three exists in `src/` today and each would enter it as an ordinary line: a settings
  class inheriting from a `BaseSettings` *subclass* (the `env_prefix` is on the parent), a nested
  `BaseModel` reached through `env_nested_delimiter`, and `Field(4, validation_alias="REAL_NAME")` —
  the last being worse than absent, because the bound is found under the prefixed field name rather
  than under the alias the environment actually reads, so the ratchet would refuse the wrong
  variable and wave the real one through. `os.getenv` and `Annotated[int, …]` were in this list and
  are closed. So was **a read through a helper**, differently and only for one helper:
  `D-2026-09-16-a-bound-with-no-off-refuses-at-import-in-one-place` put eleven bounds behind
  `mcp_server_kit.limits.env_bound`, and `_BOUND_HELPERS` follows that one *by name* — measured, the
  derived set fell 45→34 without it. A helper the scan does not know by name is still invisible,
  and that is the part left standing here. Decide whether following an alias and a parent class is
  worth the AST, or whether the honest arrangement is the floor that already exists
  (`_BOUND_ANCHORS`) plus this row.
  **Anchors:** `tests/test_fleet.py::numeric_env_bounds`,
  `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/config.py`.

- [ ] **A refusal's echo is bounded in four engines by a constant each of them declares, and most
  refusals do not go through it.** `chem`, `calc`, `safety` and `rxnpredict` each define their own
  120-character `_MAX_ECHO_CHARS` and an `_echo`/`truncate_echo` beside it, and the reason is
  recorded at `servers/calc/src/chemclaw_mcp_calc/engine/chem.py`: `connector_app` passes a
  `ValueError` to the model verbatim, so an unbounded echo is unbounded caller-influenced text in
  the context window of the turn that asked. Most refusal sites interpolate the structure directly
  instead. Re-derive the list with

  ```sh
  grep -rnE '\{[a-z_]*\.?smiles[^}]*!r\}' servers/*/src packages/*/src | grep -v '_echo\|truncate'
  ```

  The ceiling above them is not the echo bound but `mcp_server_kit.limits.MAX_SMILES_CHARS`, which
  is 4000 — so these are bounded, at roughly thirty times the bound the four engines chose.
  Measured 2026-09-12: `predict_pka` on `"C" * 1500` (inside both structural bounds, so it is an
  ordinary accepted call) raises a **1,587-character** refusal where `_echo` would have produced
  about two hundred. Two things to decide together, and that is why they are queued here as a pair: whether the
  truncation belongs in `mcp_server_kit.limits` beside the bounds it pairs with rather than
  copied per server, and whether a ratchet can tell a caller-derived echo from a corpus-derived
  one — `servers/chem/src/chemclaw_mcp_chem/engine/reagents.py` quotes a *table's* own SMILES in a
  duplicate-name error, which is not caller-influenced and needs no bound.
  **Anchors:** `packages/mcp_server_kit/src/mcp_server_kit/limits.py`,
  `servers/calc/src/chemclaw_mcp_calc/engine/chem.py`,
  `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/preprocessing.py`.

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

- [ ] **`servers/chem` has five enumerators in one cost band and a ceiling on none of them, and
  the measurement that would decide whether they need one has been taken.**
  `D-2026-09-18-an-output-cap-is-not-a-bound-on-the-work` bounded
  `enumerate_microstates`' input and deliberately added no admission ceiling, on a driven result:
  eight concurrent worst-legal calls left the event loop 384 ms late at worst against a 3 s
  `readinessProbe.timeoutSeconds`, so a ceiling would not bind. What it did *not* settle is the
  other four. Measured the same day on a 1,900-atom alkane, one call each:
  `describe_topology` **615 ms**, `enumerate_tautomer_set` **400 ms**, `enumerate_stereoisomer_set`
  **367 ms**, `enumerate_degradant_candidates` **106 ms** — all ungated. **Those four figures are a
  linear alkane's, and that shape is the cheap case for three of them.** Re-measured 2026-09-19 on
  a 996-atom PAMAM G4 dendrimer, a molecule a caller may now send:
  `enumerate_tautomer_set` **2,801 ms**, `describe_topology` **2,793 ms**,
  `enumerate_degradant_candidates` **1,823 ms**, `enumerate_stereoisomer_set` **18 ms**, against
  `enumerate_microstates`' **587 ms** — so the one with an input bound is the *cheap* one and the
  two dearest have none. Whether those four hold the interpreter the way `enumerate_microstates`
  measurably does is **not** measured and is the first thing this row owes; what the re-measurement
  settles is that "one cost band" was an artefact of the fixture, and that the worst call
  `D-2026-09-19-a-bound-on-the-site-count-prices-half-the-work` admits (**1,266 ms**) is a bigger
  number for a probe-derived ceiling to divide than the 640 ms this row was written against.
  `render_structure` is gated at
  8 while its worst *legal* depiction is 4.6 ms, which is the inversion worth resolving: either the
  band shares one ceiling derived from the probe, or `DEFAULT_MAX_CONCURRENT_RENDERS` is a knob
  `POD_THREAD_POOL_WIDTH` already makes unreachable. The row is the decision, not the number — the
  numbers above are what it is to be decided against.
  **Anchors:** `servers/chem/src/chemclaw_mcp_chem/engine/admission.py`,
  `servers/chem/tests/test_microstate_bound.py`, `servers/chem/tests/test_depiction_bound.py`.

- [ ] **Four more constant SMARTS tables in this fleet are compiled on every call, and "that was
  the last one" has now been said twice.** `D-2026-09-18-an-output-cap-is-not-a-bound-on-the-work`
  cached `servers/chem`'s `_ACIDIC`/`_BASIC` and its docstring claimed to be the third and last
  such fix; grepping `MolFromSmarts`/`ReactionFromSmarts` across `servers/*/src` in the same
  session found four more, each over a table that is a module constant:
  `species.py::enumerate_degradant_candidates` rebuilding all eleven `_TRANSFORMS` reaction SMARTS,
  `chem`'s `sites.py::_matched_atoms` and `torsions.py::_matched_pairs`, and `rxnlabel`'s
  `agents.py` and `species.py`. **None of them is measured**, which is the whole row: the one that
  was measured turned out to be worth 1.4x on a real molecule and nothing at all on a large one, so
  the useful output here is four numbers and then four `@cache`s or a note saying they are not
  worth one — not four caches applied on the strength of the pattern looking familiar.
  **Anchors:** `servers/chem/src/chemclaw_mcp_chem/engine/species.py::enumerate_degradant_candidates`,
  `servers/chem/src/chemclaw_mcp_chem/engine/sites.py::_matched_atoms`,
  `servers/chem/src/chemclaw_mcp_chem/engine/torsions.py::_matched_pairs`,
  `servers/rxnlabel/src/chemclaw_mcp_rxnlabel/engine/agents.py`,
  `servers/rxnlabel/src/chemclaw_mcp_rxnlabel/engine/species.py`.

- [ ] **The hand-written reaction classifier gates the Mixture-of-Experts priors, and the curated
  one this server already depends on is not wired to it.**
  `servers/rxnpredict`'s `engine/meta/classifier.py` is a 190-line ten-class classifier over a
  60-line `_RULES` SMARTS table; its own module docstring says to swap in Rxn-INSIGHT's classifier
  "when available", and `rxn-insight` is *already* an optional dependency of this exact server,
  already constructed in `engine/predictors/conditions/rxn_insight.py`, and already called for its
  `get_reaction_info()` dict by the sibling
  `servers/rxnlabel/src/chemclaw_mcp_rxnlabel/engine/naming.py`. That dict carries
  `CLASS` from 527 curated SMIRKS against ten hand-written rules here.
  **Three things make it a row rather than a commit, and the third is why it was not done in the
  2026-09-16 wave.** (1) `ALL_CLASSES` is a wire contract against the vendored `trust_priors.json`
  (`servers/rxnpredict/tests/test_dataset.py` checks the priors against it), so adopting Rxn-INSIGHT means writing and
  arguing a mapping from its class vocabulary onto those ten keys — a new declaration, not a
  deletion. (2) The SMARTS path has to **stay** as the no-extra fallback: `rxn_insight` is behind an
  extra the core install does not carry, and `classify_reaction` is a served tool that must answer
  without it. (3) A different class changes `effective_prior`, `consensus_score` and the candidate
  rank order, so the change cannot land without measuring that on the probe corpus — and measuring
  it needs `rxnmapper`, which means torch, transformers and a model checkpoint in a test
  environment that carries none of them. The measurement is the work.
  **That third reason is false, measured 2026-09-18.** `uv pip install
  "rxn-insight>=0.1.2"` resolves in this family's container and pulls its whole
  stack — `rxn-insight` 0.1.3, `rxnmapper` 0.4.3, `torch` 2.14.0, `transformers`
  4.57.6, 6.0 GB — and `Reaction('CC(=O)O.CCO>>CC(=O)OCC.O').get_reaction_info()`
  answers `CLASS: Acylation`, `NAME: Esterification of Carboxylic Acids`, with no
  separate checkpoint step. So the measurement is available to whoever wants it,
  and the first two reasons are the whole of what stands.
  **What a partial measurement then said, and why it is not a verdict.** Over one
  corpus the *incumbent* SMARTS table led, **97.1% to 91.3%**. That run stopped
  before the arm that could overturn it — reactions where a spectator or
  substituent carries the diagnostic group of a different class, which is the
  false-positive mode a mapping-free table is structurally prone to and an
  atom-mapped classifier is not — and the session that ran it recorded that its
  corpus so far favoured SMARTS. Two numbers on a favourable corpus are evidence
  about that corpus. They are written here because the row had none at all, and
  because they point the opposite way from the row's framing: this may be a swap
  not worth making, and the next session should expect to find that rather than
  assume the curated table wins.
  **Anchors:** `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/meta/classifier.py::ALL_CLASSES`,
  `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/predictors/conditions/rxn_insight.py`,
  `servers/rxnlabel/src/chemclaw_mcp_rxnlabel/engine/naming.py`,
  `servers/rxnpredict/tests/test_dataset.py`.

## 3 — Readiness, where it still stops

- [ ] **An `ImportError` from a broken shared library reads as an extra nobody installed, and only
  one of the two servers catches it.** `degradation.classify` sorts every `ImportError` as
  `not_installed`, which is right for `ModuleNotFoundError` and wrong for
  `ImportError("libcudart.so.11: cannot open shared object file")` — a distribution that *is*
  installed and cannot load, which is a broken image. Distinguishing them from the exception is not
  possible and matching the message is the control `degradation.py`'s docstring refuses to write, so
  the only reliable test is the one `rxnlabel` already makes: `version._installed(distribution)`
  against `available()`, where metadata saying the distribution is present and the predicate saying
  it did not build is the fault. `rxnpredict` has no equivalent — a predictor class carries
  `extras_install`, which is an extra's name rather than a distribution's, so the cross-check needs a
  third declaration per predictor and that is the thing to design rather than bolt on.
  **Anchors:** `packages/mcp_server_kit/src/mcp_server_kit/degradation.py::classify`,
  `servers/rxnlabel/src/chemclaw_mcp_rxnlabel/engine/readiness.py`,
  `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/predictors/base.py`.

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

## 4 — The gate itself

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

## 5 — Corpora that are not yet licensed to exist

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

- [ ] **No mirrored corpus has a named refresh owner or cadence.** Every `dataset.json` carries
  `retrieved_from` and a `sha256`, so what a corpus *is* can be checked; when it was last true of
  the upstream cannot. `CLAUDE.md` already says refreshing a snapshot is a build step reviewed by a
  person, and `MODULES.md` says the owner and cadence go in that server's README — no README carries
  either today. A stale patent index nobody knows is stale is worse than no patent index, and the
  same is true of a hazard table. Decide the smallest thing that works: a field in `dataset.json`
  (which `load_dataset` already refuses to open without its six keys) is checkable; a sentence in a
  README is not.
  **Anchors:** `MODULES.md`, `packages/mcp_server_kit/src/mcp_server_kit/datasets.py`,
  `servers/props/src/chemclaw_mcp_props/data`.

## 6 — Consuming a server hosted elsewhere

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

## 7 — The coverage floor, and what it is a floor over

## 8 — Correlations that need data nobody here has

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
