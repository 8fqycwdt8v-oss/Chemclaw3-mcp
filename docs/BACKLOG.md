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

- [ ] **Two of the four channels outside the runtime guard are covered by nothing `make check`
  runs.** `egress.py` names four channels it cannot reach by construction: a child process, a
  `ctypes` call into `libc`, the private C type `_socket.socket`, and any syscall from a compiled
  extension. Three of those arrive as an *import*, which is what the static scan reads, and it
  refuses two: `_socket`, and a named compiled extension (`grpc` since `c1772fb`). The other two
  — `ctypes`, off that list deliberately because `servers/pyexec`'s sandbox needs it for
  `prctl(PR_SET_DUMPABLE, 0)`, and a child process, which no static reader can help with because
  `subprocess` is how `pyexec` and `calc` work — are covered only by
  `make offline-run`, which takes the network namespace away. That target is **not** in `make
  check`: it needs `unshare`, so CI runs it as its own step and a local gate can be green without
  it. Decide whether the Makefile can detect `unshare` and fold it in, or whether the honest
  arrangement is a `make check` that says which layer it did not run — the shape
  `tests/test_backlog_register.py` uses for the rows it cannot open.
  **Anchors:** `packages/mcp_server_kit/src/mcp_server_kit/egress.py`,
  `packages/mcp_server_kit/src/mcp_server_kit/no_egress.py`, `scripts/offline_check.py`, `Makefile`.

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

- [ ] **The build backend every wheel is built with is outside `uv.lock`, and therefore outside
  `make deps-audit`.** `uv build` resolves `build-system.requires` per build, not from the locked
  closure — `grep -n 'name = "hatchling"' uv.lock` answers nothing — so the supply-chain control
  `D-2026-09-13-an-audit-of-a-lockfile-no-image-reads-audits-nothing` built cannot see it.
  `tests/test_fleet.py::test_every_server_builds_a_wheel_that_carries_its_data` now passes
  `--offline`, which stops that build reaching an index
  (`D-2026-09-14-a-child-process-is-outside-the-guard-and-uv-build-is-one`), and that is a no-egress
  fix rather than an audit one: what it now builds with is whatever version the cache happens to
  hold. The exposure is bounded — those wheels are never shipped, since an image installs the
  exported closure with `--require-hashes` — so the open question is whether a build dependency is
  worth pinning at all here, and if so whether the honest place is a `[tool.uv] constraint`
  the export can carry rather than a second lock nothing reads.
  **Anchors:** `uv.lock`, `pyproject.toml`, `tests/test_fleet.py::test_every_server_builds_a_wheel_that_carries_its_data`,
  `Makefile`.

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

- [ ] **One bound is a `float` and is therefore still read the way the unguarded `int` ones were.**
  `D-2026-09-16-a-bound-with-no-off-refuses-at-import-in-one-place` put every integer bound behind
  `mcp_server_kit.limits.env_bound`, which is `int`-typed. `servers/props`'
  `CHEMCLAW_PROPS_MAX_TB_RATIO` is a ratio, so it stayed a bare
  `float(os.environ.get("CHEMCLAW_PROPS_MAX_TB_RATIO", "1.8"))` — and it has the same defect eight
  of those eleven had: it multiplies a normal boiling point in kelvin, so `0` makes the ceiling
  −273.15 °C and every vapour-pressure question is refused, on a pod that starts and passes
  readiness. Re-derive the set with

  ```sh
  grep -rnE 'float\(os\.environ' packages/*/src servers/*/src --include=*.py
  ```

  which finds exactly this one today. What is *not* obvious is the remedy: a second `env_ratio`
  with one caller is the abstraction the Rule of Three says to inline, and widening `env_bound` to
  `int | float` makes its `minimum` and its return type ambiguous at eleven call sites that do not
  need it. Decide between those two and a third — that this bound's floor is a `Field(gt=0)` if
  `props` ever grows a settings object — rather than copying the helper.
  **Anchors:** `servers/props/src/chemclaw_mcp_props/engine/correlations.py`,
  `packages/mcp_server_kit/src/mcp_server_kit/limits.py`.

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

- [ ] **One SMARTS table in the fleet is still compiled on every call, and it is the expensive
  one.** `servers/chem`'s `engine/species.py::_sites` runs `Chem.MolFromSmarts` over all eleven
  `_ACIDIC`/`_BASIC` patterns on each invocation. `servers/safety`'s `screen.py::_load_rules` is
  `lru_cache`d over its whole rule table and says why in its docstring; `servers/rxnpredict`'s
  `classifier.py::_compiled` was fixed on 2026-09-16 and is measured at 1.11-1.83x. This one is
  worth more: measured 2026-09-16 on tyrosine, both tables through `_sites` cost **440 µs** with
  the per-call compile and **31 µs** against pre-compiled patterns, against a whole
  `enumerate_microstates` call of **1,443 µs** — so roughly 28% of that call is re-parsing
  constants. The fix is four lines and the reason it is a row rather than a commit is that
  `enumerate_microstates` is `chem`'s heaviest tool and nothing here bounds or measures its latency,
  so the honest order is a bound first (the section above) and then the saving. A `@cache` keyed on
  the SMARTS string, as `classifier.py` now does, is the shape.
  **Anchors:** `servers/chem/src/chemclaw_mcp_chem/engine/species.py::_sites`,
  `servers/safety/src/chemclaw_mcp_safety/engine/screen.py::_load_rules`,
  `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/meta/classifier.py::_compiled`.

- [ ] **`servers/calc`'s 500-atom cap is unchanged and the measurement that set it is retired.**
  `CHEMCLAW_XTB_MAX_ATOMS` was derived from the ANC preconditioner's dense `(3N, 3N)` model Hessian
  — 3.6 s at 120 atoms, 11.6 s at 240, 32.9 s and 18.7 MB at 510, 127 GB at the ~42,000 atoms a
  1 MB body can carry. `D-2026-09-16-the-driver-is-a-command-line-program-the-optimizer-is-not`
  deleted that preconditioner; geomeTRIC's coordinate system is dense over 3N as well, so the
  *shape* of the argument carries and none of the constants do. The number was deliberately not
  changed in that commit — moving a bound on an unmeasured basis is worse than leaving one whose
  basis is stated as retired — so what is open is the measurement: geomeTRIC's coordinate-system
  build plus one optimizer cycle at 120, 240 and 510 atoms, against the same 1 MB body cap, and
  then whether 500 is still the right place for it.
  **Anchors:** `servers/calc/src/chemclaw_mcp_calc/engine/config.py`,
  `servers/calc/tests/test_cost_bounds.py`,
  `servers/calc/src/chemclaw_mcp_calc/engine/xtb_opt.py::_coordinate_system`.

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

- [ ] **Two servers answer a corrupt corpus with a crash loop rather than a 503, and the difference
  is what an operator can see.** Driven on 2026-09-12 by mutating each vendored corpus in place:
  `chem` and `safety` answered **503** naming the table and both hashes, while `props` and
  `rxnpredict` raised `DatasetError` at *import* — `props`'s `tools.py` calls
  `len(records.all_solvents())` at module scope, and `rxnpredict`'s settings load pulls
  `trust_priors.json` in. Neither pod ever serves, so neither is dangerous; what is lost is the
  reason, which reaches a kubelet as `CrashLoopBackOff` and a container log instead of as a probe
  body naming the two hashes. Decide whether a corpus load belongs behind the probe on every server
  (which means `props` giving up the incidental module-scope load its own `_readiness` docstring
  already calls an accident) or whether a crash loop is the honest answer for a corpus that cannot
  be read at all.
  **Anchors:** `servers/props/src/chemclaw_mcp_props/tools.py`,
  `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/config.py`,
  `packages/mcp_server_kit/src/mcp_server_kit/datasets.py`.

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

- [ ] **Every image still takes its *build backend* from pip's isolation, unhashed, at build time.**
  `D-2026-09-16-a-dependency-with-no-wheel-builds-under-whatever-pip-fetches-that-day` closed this
  for the one dependency that is actually built from source — `geometric`, the only sdist-only entry
  in `uv.lock` — by exporting the lock's `build` dependency group and passing
  `--no-build-isolation` to `servers/calc/Containerfile`'s first `pip wheel`. The **second** pass in
  every Containerfile is untouched: `python -m pip wheel --no-deps ./packages/mcp_server_kit
  ./servers/<name>` builds two `hatchling`-backed distributions, and pip resolves `hatchling` (and
  its own `hatchling` dependencies) from PyPI at that moment — chosen that day, no hashes, outside
  the lock, executing a build backend. Nothing installed that way reaches a shipped image, which is
  why it is here and not above the `rxnlabel` row: what is at stake is unpinned code running in the
  build and a wheel whose bytes depend on when it was built, not the runtime closure. Closing it is
  `hatchling` in the `build` group plus the same two lines in twelve Containerfiles, and the reason
  it is not done in the commit that found it is that twelve edits to close a fleet-wide property is
  a change that wants its own measurement — specifically, whether a hatchling in the build
  environment can change what `hatchling.build` puts in a wheel.
  **Anchors:** `servers/calc/Containerfile`, `pyproject.toml` (`[dependency-groups] build`),
  `tests/test_fleet.py::test_a_sdist_only_dependency_builds_under_a_pinned_backend`.

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

- [ ] **`make type` does not check the test tree, and 146 errors are waiting in it.** `$(SRC)`
  lists the `src/` roots and no test directory, so `mypy --strict` never reads the files that drive
  every ratchet in this repository. Chemclaw3 by contrast types `src`, `examples` and `tests`.

  **The hard half is solved and the estimate was wrong, both measured 2026-09-15.** The invocation
  that reads the tree is `MYPYPATH=. mypy --strict --explicit-package-bases --namespace-packages
  tests packages/*/tests servers/*/tests`: keying modules by path rather than basename is what gets
  past `Duplicate module named "test_no_egress"`, which ten servers now trigger. Run that way, the
  tree reports **146 errors in 31 files**, not the one this row used to claim — the figure was one
  because that was all anybody had checked, on the single file they could invoke mypy on without
  hitting the collision.

  **The yield looks low, which is why this is still a row rather than a commit.** 38 of the 146 are
  `no-untyped-def` on test helpers and 58 are `attr-defined`, mostly RDKit's unstubbed module
  surface and `object` returned by untyped fixtures. Twenty-two were read individually and **none
  was a live defect**: the two that looked like one are a loop variable rebound to a different type
  later in the same scope (`tests/test_consumer_agreement.py` at line 276, which runs correctly and
  types inconsistently) and an `int | None` compared with `>` that is never `None` for that input
  (`servers/chem/tests/test_species.py` at line 146, which would raise `TypeError` rather than
  fail its assertion if a regression made it `None`).

  So the decision this row now needs is whether 146 fixes with a measured-low defect yield is worth
  the gate, or whether a narrower strictness for tests is — and the second is the one to be careful
  about, since dropping `--disallow-untyped-defs` alone removes 38 of the errors without removing
  any of the risk.

  **What it did find, on the three servers added since:** nine `# type: ignore[arg-type]` comments
  in `servers/thermalsafety/tests/test_semenov.py` that suppressed nothing — the same "claim a
  control exists" shape this repository keeps deleting, one layer down. Those are gone, and
  `servers/kinetics`, `servers/suitability` and `servers/thermalsafety` are clean under the
  invocation above, so the 146 is entirely older code.
  **Anchors:** `Makefile`, `servers/calc/tests/test_admission.py`, `pyproject.toml`.

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

- [ ] **Eight `calc` tools are hardcoded in a third module neither repository checks.**
  **Other repository:** `Chemclaw3`. Its `tests/test_sibling_manifest_agreement.py` lists two
  callers — `connectors/calc/compose.py` and `remote.py` — and finds 13 hardcoded call sites naming
  10 tools. Running that file's own AST walker over `src/chemclaw/connectors/calc/server/tools.py`
  on 2026-09-14 found **11 more sites naming 10 tools, 8 of them named by no checked module**:
  `compute_atomic_descriptors`, `compute_electronic_properties`, `compute_surface_potential`,
  `compute_xtb_energy`, `predict_pka`, `predict_solubility`, `predict_site_reactivity`,
  `predict_developability_profile`. Every one is served and every argument declared, so the
  contract is sound and **unwatched** — a rename in `servers/calc/` would reach all eight with no
  test on either side. Checked and unchecked together name 18 of the 20 tools
  `servers/calc/tool-surface.json` records; `optimize_geometry` and `predict_logd` are named by no
  hardcoded dict-literal site in any of the three modules, which is worth confirming rather than
  assuming when this is worked. The fix is that repository's `_CALLERS` tuple and nothing here can
  make it; this row is what keeps it from being forgotten.
  **Anchors (Chemclaw3):** `tests/test_sibling_manifest_agreement.py::_CALLERS`,
  `src/chemclaw/connectors/calc/server/tools.py`.

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

- [ ] **`[tool.coverage.run] source_pkgs` names eight distributions and this workspace ships
  twelve.** `kinetics`, `suitability`, `thermalsafety` and `unitops` are all absent from it, so the
  88% floor is measured over a basis that excludes four built servers outright — and the comment
  above the list still opens "The eight distributions this workspace ships", which was true when it
  was written and is a claim about a commit rather than about `HEAD`. That comment also states the
  exact failure this causes: a package nobody imported "is the one case a floor exists to catch",
  and four of them are now invisible to it by name rather than by import. **This is not something
  `unitops` introduced** — it arrived with the three servers before it, and the row is filed with
  `unitops` because that is the commit that noticed. Changing the basis changes the percentage, so
  it is a measurement before it is an edit: add the four, run `make cov`, and either the floor holds
  and the list is simply corrected, or it moves and the number is re-derived in the same commit the
  way 88 was. What must not happen is one server being added to the list and the prose still saying
  eight.
  **Anchors:** `pyproject.toml`, `Makefile`.

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
