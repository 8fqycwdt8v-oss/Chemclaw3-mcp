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
`Ports` section published Chemclaw3's connector range as 8810–8815 while its `bo` connector sat on
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

## 2 — The resource-bound ratchet, where it stops

- [ ] **Neither ratchet can see a pod `env:` a cluster operator adds outside these files.** Both
  read the tree: `servers/*/deploy/*.yaml` and `servers/*/Containerfile`. A bound moved by a kustomize
  overlay, a Helm value in a deploying repository, or an operator's `kubectl set env` is invisible
  here and always will be — the shipped files are what this suite can read. What is not yet decided
  is whether the serving side should *say* what it is running: an admission ceiling and an atom
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

- [ ] **The bound derivation reads two configuration mechanisms and four shapes past them are
  invisible, one of them under the wrong name.** Measured 2026-09-12 against synthetic modules, none
  of these shapes exists in `src/` today and each would enter it as an ordinary line: a read through
  a helper (`_env_int("X", 4)`), a settings class inheriting from a `BaseSettings` *subclass* (the
  `env_prefix` is on the parent), a nested `BaseModel` reached through `env_nested_delimiter`, and
  `Field(4, validation_alias="REAL_NAME")` — the last being worse than absent, because the bound is
  found under the prefixed field name rather than under the alias the environment actually reads, so
  the ratchet would refuse the wrong variable and wave the real one through. `os.getenv` and
  `Annotated[int, …]` were in this list and are closed. Decide whether following an alias and a
  parent class is worth the AST, or whether the honest arrangement is the floor that already exists
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

- [ ] **`Admission` is copied into five servers, and "one server never imports another" is not the
  only reason it could be.** `calc`, `chem`, `pyexec`, `rxnlabel` and `rxnpredict` each carry a
  ~40-line lock-and-counter class whose bodies are near-identical; what genuinely differs is the
  *refusal wording* (`chem`'s names a replica because raising its ceiling cannot help, `calc`'s
  names a knob and carries `AT_CAPACITY_MARKER`) and, in three of them, the cost model. The stated
  reason for copying is that one server never imports another — which is true and does not apply to
  `mcp_server_kit`, the package every one of them already imports. Decide whether the counter and
  the clamp belong there with each server keeping its own message, or whether five copies is the
  right price for five independent dependency closures. Re-derive the list with
  `grep -rln "class Admission" servers` before working it, because a sixth may have arrived.
  **Anchors:** `servers/calc/src/chemclaw_mcp_calc/engine/admission.py`,
  `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/admission.py`,
  `packages/mcp_server_kit/src/mcp_server_kit/limits.py`.

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

- [ ] **Five environment-read bounds still accept a value that silently breaks the server.**
  `D-2026-09-12-a-bound-that-can-be-set-to-zero-has-to-say-what-zero-means` fixed the three W23
  added — a batch bound of `0` started a `rxnlabel` pod that passed readiness and refused every
  call — and left the older ones as they were: `MCP_MAX_SMILES_CHARS` and `MCP_MAX_MOLECULE_ATOMS`
  in the kit, `CHEMCLAW_CHEM_RENDER_SIZE_PX`, `CHEMCLAW_CHEM_MAX_DEPICTION_ATOMS` and
  `CHEMCLAW_CHEM_MAX_DEPICTION_CHARS` in `chem`, plus `CHEMCLAW_SAFETY_MAX_COMPONENTS`. Each is a
  bare `int(os.environ.get(...))` that accepts `0` and negatives. The obvious fix — one
  `env_int(name, default)` helper — is the one thing that must not be done without a second change:
  `tests/test_fleet.py::test_the_bound_scan_sees_both_configuration_mechanisms` pins that the
  ratchet's inventory deliberately does not follow a read through a helper, so converting these
  would take all seven out of the ratchet that exists to watch them. So the row is two decisions in
  order: whether the scan should follow one named helper, and only then whether to share the check.
  **Anchors:** `tests/test_fleet.py::_numeric_environ_reads`,
  `packages/mcp_server_kit/src/mcp_server_kit/limits.py`,
  `servers/chem/src/chemclaw_mcp_chem/engine/depiction.py`.

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

- [ ] **`calc`'s probe now covers the xTB backend and says nothing about CREST.**
  `D-2026-09-12-a-readiness-check-that-does-not-run-the-thing-is-not-a-readiness-check` closed the
  `xtb-absent` key, which was dangerous because it wrote a *wrong record* into Chemclaw3's ledger.
  `crest_cli.binary_version()` has the same `"absent"` shape, but a CREST call on a pod without the
  binary raises `CliError` naming it — an honest failure rather than a corrupted key — so it was
  left out rather than folded in. What is still true is that such a pod reports ready and cannot
  serve `search_conformers` or `search_complex`. Decide whether a pod that can serve thirteen of
  fifteen tools is ready, which is a question about partial capability that no other server in this
  fleet has had to answer yet.
  **Anchors:** `servers/calc/src/chemclaw_mcp_calc/engine/crest_cli.py::binary_version`,
  `servers/calc/src/chemclaw_mcp_calc/app.py::_readiness`.

## 4 — The gate itself

- [ ] **`make type` does not check the test tree, and there is an error waiting in it.** `$(SRC)`
  lists eight `src/` roots and no test directory, so `mypy --strict` never reads the files that
  drive every ratchet in this repository. Measured on 2026-09-12: `mypy --strict
  servers/calc/tests/test_admission.py` reports `Item "None" of "Tool | None" has no attribute "fn"`
  at line 292 — pre-existing, present on `origin/main` too. Chemclaw3 by contrast types `src`,
  `examples` and `tests`. Closing this is two steps, and the second is the reason it is a row rather
  than a one-liner: fix that error, and then find the invocation that can read the tree at all —
  `mypy --strict tests packages/*/tests servers/*/tests` aborts before checking anything with
  `Duplicate module named "test_no_egress"`, because every server ships a file of that name and
  mypy keys modules by basename. The suite already solves the same collision with pytest's
  `--import-mode=importlib`; mypy's equivalent is `--explicit-package-bases` with `MYPYPATH`, or
  per-root invocations.
  **Anchors:** `Makefile`, `servers/calc/tests/test_admission.py`, `pyproject.toml`.

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
