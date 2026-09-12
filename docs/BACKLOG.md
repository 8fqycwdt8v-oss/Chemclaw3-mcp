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

## 3 — The gate itself

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
