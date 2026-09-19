# The gate. `make check` is what CI runs; nothing here needs a cluster, a database or a network.
# `deps-audit` is the one exception and says so: it asks an advisory database a question no local
# copy can answer.
#
# bash rather than the default shell, and errexit/pipefail with it: `deps-audit` classifies the
# output of a command that exits 1 for two different reasons, and a pipeline whose first stage fails
# must not read as a clean scan.
SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c

.DEFAULT_GOAL := help
UV ?= uv
SRC := packages/mcp_server_kit/src servers/props/src servers/chem/src servers/safety/src servers/calc/src servers/pyexec/src servers/rxnlabel/src servers/rxnpredict/src servers/kinetics/src servers/suitability/src servers/thermalsafety/src servers/unitops/src scripts $(wildcard servers/*/scripts)
# The test tree, globbed rather than listed: a new server's tests are checked the day the directory
# exists, which is the half `SRC` gets wrong by being a list somebody has to remember to extend.
# `conftest.py` is named because nothing globs it: it is layer 3 of the no-egress posture — the
# fixture that arms the guard for the whole suite — and it was outside the gate, as was `scripts/`,
# which is all of `make offline-run`. `servers/*/scripts` is globbed for the reason the test tree
# is: it held the one `.py` in this repository that `mypy --strict` had never read, and a hand list
# is what could not see it (`D-2026-09-18-every-py-in-the-tree-or-a-named-exemption`).
TESTS := conftest.py tests $(wildcard packages/*/tests) $(wildcard servers/*/tests)

.PHONY: help
help: ## Show this help.
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.PHONY: install
install: ## Sync the workspace (every package, plus dev dependencies).
	$(UV) sync

.PHONY: lint
lint: ## ruff check + format check.
	$(UV) run ruff check .
	$(UV) run ruff format --check .

.PHONY: format
format: ## Apply ruff's fixes and formatting.
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

.PHONY: type
type: ## mypy --strict over every server, the shared kit and the test tree.
	@# **`--explicit-package-bases` is what makes the test tree checkable at all.** Ten servers ship
	@# a `tests/test_no_egress.py`, and mypy keys a module by its basename unless told otherwise, so
	@# the plain invocation dies on `Duplicate module named "test_no_egress"` before it checks
	@# anything. That flag keys by path instead, rooted at the working directory — which is this
	@# repository, because make chdirs before it runs a recipe.
	@#
	@# This line carried `MYPYPATH=.` in front of it and a sentence calling it the other half of
	@# that fix. It was a no-op: `env -u MYPYPATH` over the same invocation is byte-identical
	@# `Success`, because `.` is already the root the flag falls back to and `[tool.mypy] mypy_path`
	@# names the twelve `src/` trees. Dropping the flag *does* break the run, so only the variable
	@# was dead (`D-2026-09-18-a-ratchet-that-observes-half-a-command-holds-half-a-gate`). The flag
	@# is asserted by `tests/test_fleet.py`, which is why removing the variable removes no control.
	@#
	@# One invocation rather than two: mypy builds one graph, and the test tree imports the source
	@# tree anyway, so splitting them would analyse the same modules twice.
	$(UV) run mypy --explicit-package-bases --namespace-packages $(SRC) $(TESTS)

.PHONY: test
test: ## The whole suite, with the egress guard armed (see conftest.py).
	$(UV) run pytest -q

.PHONY: no-egress
no-egress: ## The no-egress checks alone — the static scan, the runtime guard, the NetworkPolicy.
	$(UV) run pytest -q -k "egress or deploy"

.PHONY: manifest-validate
manifest-validate: ## Every connector.yaml parses, is classified, and matches its running server.
	$(UV) run pytest -q -k "manifest or test_server"

.PHONY: offline-run
offline-run: ## Prove every server answers with the network taken away (needs unshare; Linux).
	unshare --user --map-root-user --net -- $(UV) run python scripts/offline_check.py -q

.PHONY: cov
cov: ## The suite again, with coverage measured and the floor in `pyproject.toml` enforced.
	@# One run, not two: `--cov` costs nothing measurable here (measured 176 s against 210 s for the
	@# bare run, which is noise), so `check` calls this instead of `test` rather than paying for the
	@# suite twice. `test` stays for a fast bare run while iterating.
	$(UV) run pytest -q --cov --cov-report=term

# The fourth egress layer, run where the kernel permits it and **named** where it does not.
#
# `egress.py` lists four channels the runtime guard cannot reach by construction: a child process, a
# `ctypes` call into `libc`, the private C type `_socket.socket`, and any syscall from a compiled
# extension. The static scan refuses two of them (`_socket`, and a named compiled extension);
# `ctypes` is off its list deliberately, because `servers/pyexec`'s sandbox needs it, and a child
# process is what `pyexec` and `calc` *are*. So those two are covered by exactly one thing —
# `offline-run`, which takes the network namespace away instead of asking Python nicely — and it
# was outside `make check`, so a local gate went green with two of the four unverified and nothing
# on screen saying so.
#
# **Folding it in costs CI nothing**, which is what makes this the cheap answer rather than a
# trade: `.github/workflows/ci.yml` calls `make lint`, `make type` and `make cov` as separate
# steps and runs `offline-run` as its own job. It never invokes `make check`.
#
# Guarded rather than unconditional, because `unshare --net` needs unprivileged user namespaces and
# a container can be configured without them. A gate that *fails* there would be telling a
# contributor their change is broken when it is their kernel; a gate that silently omits the step is
# the thing this target exists to stop. So it says which layer it did not run, in the same shape
# `deps-audit` below uses for an audit it could not reach.
.PHONY: offline-guarded
offline-guarded:  ## `offline-run` where the kernel allows a network namespace, a named notice where not.
	@if unshare --user --map-root-user --net -- true >/dev/null 2>&1; then \
		$(MAKE) --no-print-directory offline-run; \
	else \
		printf '\nSKIPPED offline-run: this kernel refuses an unprivileged network namespace.\n'; \
		printf 'The two egress channels no static scan reaches - a child process and a `ctypes`\n'; \
		printf 'call into libc - are unverified by this run. CI runs them as its own job.\n\n'; \
	fi

.PHONY: check
check: lint type cov offline-guarded deps-audit ## Everything CI runs, including the offline lane where the kernel allows it.
	@# `deps-audit` is last on purpose: a dependency finding is a real failure, but not one that
	@# should mask a broken test, and it is the only step here whose fix lives in `uv.lock` rather
	@# than in the diff under review. It is in this list at all because CI now runs it, and a local
	@# gate that skips the supply chain makes "a green `make check` means a green CI" false for
	@# exactly the class of defect nobody would think to look for. With no network it reports
	@# SKIPPED and says so rather than failing — see the target.

# The two patterns that classify `deps-audit`'s output. Named here rather than inlined so the recipe
# reads as the decision it is: a finding and an outage are different events that `pip-audit` reports
# with the same exit code (1).
#
# A real finding. Checked first and never excused, so an advisory whose own text mentions a
# connection failure cannot buy an exemption.
AUDIT_FOUND := Found [0-9]+ known vulnerabilit
# The advisory database (or `pip-audit` itself) could not be reached. Both observed forms: `uvx`
# failing to fetch the tool, and `pip-audit` dying inside `requests`.
AUDIT_UNREACHABLE := ConnectionError|Failed to fetch|Max retries exceeded|Temporary failure in name resolution|Name or service not known|Network is unreachable

# **The advisories this gate does not fail on, and the argument for each.** A suppression list is
# itself a claim, so the argument is here, in the file that reads it, rather than in a prose
# document — and it is deliberately short: everything on it turns on a **malicious artefact on
# disk** — an unpickled cache, a crafted `config.json`, a checkpoint index — which is the one class
# this fleet's posture already answers, and nothing on it is reachable from a request.
#
# **The ids themselves are in `pyproject.toml`, and `AUDIT_IGNORE` below is derived from them.** Not
# for tidiness: each row there carries the package the argument is about and the version `uv.lock`
# resolved when it was written, and `tests/test_deps_suppressions.py` fails when the lock moves one.
# Eight rows, and the audit's own line says `13 ignored`. **That gap is duplication, not aliasing,
# and this comment said the opposite for a wave.** Measured 2026-09-14 against this lock, with no
# suppressions passed: 13 findings over **eight distinct advisory ids** — five of the eight are
# returned *twice* (`PYSEC-2026-2447`, `-3447`, `-2288`, `-2289`, `-2290`), three once. The
# duplication is the advisory service's rather than the export's: the export carries one
# `diskcache==5.6.3` line, and a one-line requirements file holding only it
# (`uvx pip-audit --no-deps --disable-pip -r one.txt`) returns that advisory twice. So an exact-id
# row with no alias in play
# already silences two — `--ignore-vuln PYSEC-2026-2447` alone reports `ignored 2`.
#
# **Alias matching is real and is a different fact about a different pair.** `--ignore-vuln` matches
# by id *or any alias*, and two rows here name an id this closure does not report under
# (`GHSA-xrqw-3rrv-vx5w` → `PYSEC-2026-3929`, `CVE-2026-69112` → `PYSEC-2026-3804`); passing just
# those two reports `ignored 2`, one finding each. Aliasing therefore explains **2 of the 13** and
# **none** of the 8→13 gap. The difference matters to whoever reads this before a deployment: the
# old sentence taught that a suppression can quietly cover advisories nobody listed, where what
# actually happens is duplicate records under ids this table does list.
#
# **`13` is a dated observation of a third-party service and nothing in the suite pins it** — the
# suite runs with the egress guard armed, so the only thing that could ask is `make deps-audit`
# itself, and the number will move on the advisory database's schedule rather than this repository's.
# What is pinned is local and is the pair that matters: eight rows, each naming a package and the
# version `uv.lock` still resolves.
#
# What id-or-alias matching cannot notice is a
# dependency being *fixed*: before the table, a fixed dependency merely stopped being reported and
# the suppression outlived its reason with nothing to say so — which the `accelerate` paragraph
# below stated as a known hole
# (`D-2026-09-13-a-suppression-with-no-expiry-outlives-its-argument`).
#
# Every entry below lives in the optional ML extras of two servers (`rxnlabel[models]`,
# `rxnpredict[reaction_t5,rxn_insight]`) and none appears in the closure without them — measured:
# `--all-packages --no-dev` alone reports zero. They are audited anyway because those extras are
# what the *images* install.
#
#   PYSEC-2026-2447   diskcache 5.6.3, no fix released. Pickle deserialization by an attacker with
#                     write access to the cache directory. Reachable only through `rxn-utils`, in a
#                     rootless image whose model tree is mounted read-only.
#   PYSEC-2026-3447   setuptools 80.10.2, fixed in 83.0.0 — which nothing in this closure can take
#                     yet (`uv lock --upgrade-package setuptools` resolves to the same version, held
#                     by `rxn-insight==0.1.3`'s `setuptools<81` cap; not torch's — Chemclaw3 carries
#                     setuptools 83 with the same torch). The defect is in `FileList`'s MANIFEST.in
#                     matching when *building an sdist*; no server runs a build at request time or at
#                     all.
#   PYSEC-2025-217    transformers 4.57.6, no fix released. RCE via a crafted X-CLIP *checkpoint
#                     conversion*, which is a build-time script here and not shipped.
#   PYSEC-2026-2288   transformers, fixed in 5.0.0. `Trainer._load_rng_state` calls `torch.load`
#   PYSEC-2026-2289   transformers, fixed in 5.3.0. A crafted `config.json` reaching a Hub repo
#   PYSEC-2026-2290   transformers, no fix / 5.5.0. LightGlue loading honours a remote code path
#   GHSA-xrqw-3rrv-vx5w  transformers, fixed in 5.10.0 (CVE-2026-9856, PYSEC-2026-3929 — the
#                     spelling this closure's audit actually reports it under). `save_pretrained` uses
#                     `chat_template` keys as filenames unvalidated, so a crafted
#                     `tokenizer_config.json` escapes the save directory
#                     — all four require the library to *fetch* an attacker-controlled repository.
#                     These images bake their weights at build time, run with `HF_HUB_OFFLINE=1`
#                     and `TRANSFORMERS_OFFLINE=1` (both, in both Containerfiles), and sit behind a
#                     default-deny NetworkPolicy with the egress guard armed: there is no path from
#                     a request to a Hub fetch. **`/opt/models/SHA256SUMS` was cited here as a
#                     fourth control and is not one**: `rxnpredict` writes it, `rxnlabel` writes no
#                     manifest at all, and the whole repository mentions the path exactly once — the
#                     line that creates it. Nothing reads it, at build time or at run time, which is
#                     this repository's own "a README is not a gate" with a checksum for a subject. The fourth needs one hop more than the other three
#                     and this fleet takes neither: no server calls `save_pretrained` at all, in
#                     `servers/` or in `packages/` — a serving process reads weights, it does not
#                     write them. `transformers>=5` is a major bump across a forked predictor stack,
#                     so it is a measured migration rather than a lockfile edit — the `uv` updater in
#                     `.github/dependabot.yml` is what proposes it, and 5.10.0 is further out than
#                     the 5.0 the three above already wait on rather than a new reason to move.
#   CVE-2026-69112    accelerate 1.14.0 (PYSEC-2026-3804 — again the spelling the audit reports),
#                     **no fix released — and 1.15.0 is not one**. Path traversal
#                     in `load_checkpoint_in_model` / `load_checkpoint_and_dispatch`: a sharded
#                     checkpoint's `weight_map` values are joined onto the checkpoint folder and
#                     never sanitised, so a `../` or absolute entry reads an arbitrary file and a
#                     named-pipe entry blocks the loader indefinitely. **Check the range before
#                     bumping.** `pip-audit` goes quiet on `accelerate==1.15.0` — measured — but
#                     1.15.0 was uploaded 2026-09-09, a day after the advisory was last modified, so
#                     the range stops at 1.14.0 because 1.15.0 did not exist, not because it is
#                     fixed: `load_checkpoint_in_model` is **byte-identical** between the two wheels
#                     (same bare `os.path.join(checkpoint_folder, f)`, no `realpath`, no
#                     `commonpath`, no FIFO check), and upstream's fix commit is in no release yet.
#                     A bump to 1.15.0 would buy a green gate and ship the same function.
#                     Unreachable here for a reason that needs none of that: **nothing calls the
#                     vulnerable API.** `accelerate` is imported nowhere in `servers/` or
#                     `packages/` — it enters one shipped image because `rxnpredict[reaction_t5]`
#                     declares it (and the `t5chem` extra reaches it again, which no image
#                     installs). The indirect route is shut too: the locked
#                     transformers 4.57.6 names both functions only in a docstring
#                     (`integrations/accelerate.py`), and no server passes `device_map` anywhere.
#                     The three `from_pretrained` call sites under
#                     `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/predictors/forward/`
#                     take a SHA-pinned repo id or a configured directory, and no tool argument is a
#                     path — `tools.py` takes SMILES plus a model name clamped to the served set.
#                     Were it called, the index it would read is the one baked at build time from a
#                     commit-SHA-pinned repo (`scripts/fetch_models.py`, held equal to the predictor
#                     by `tests/test_model_pin.py`), in a pod with `readOnlyRootFilesystem: true`
#                     whose one writable mount is an `emptyDir` at /tmp
#                     (`servers/rxnpredict/deploy/deployment.yaml`): nothing at runtime can plant a
#                     crafted `*.index.json` or a FIFO under `/opt/models`. The advisory's own
#                     `AV:L/UI:P` is that conclusion from the other side. **Nothing here goes red
#                     when a fix ships**, and the header's "an entry whose package no longer
#                     resolves to the version below is an entry to delete" does not fire for this
#                     one: `--ignore-vuln` matches by id, so a fixed `accelerate` merely stops being
#                     reported, and no test compares this version to the lock (`rxnpredict`'s image
#                     installs the extra, not `accelerate==` from the index, so
#                     `test_an_image_that_installs_from_the_index_pins_what_the_audit_read` never
#                     sees it). What retires this entry is a reader of the weekly `uv` bump
#                     re-deriving the list — and what settles it is the byte-diff above, not the
#                     audit's silence. **That paragraph described the hole this file now has a
#                     mechanism for**: the row in `pyproject.toml` pins this argument to
#                     `accelerate==1.14.0`, so the *next bump* — to 1.15.0 or past it — turns the
#                     suppression red and brings a reader back to this text, whether or not the
#                     advisory has moved. Dependabot alerts are the second trigger and are outside
#                     this file: they are enabled on this repository and unaffected by
#                     `--ignore-vuln`, so a patched `accelerate` also becomes a security-update pull
#                     request here.
# Derived, so the ids exist once. An extraction that fails yields an empty list, which makes the
# audit *stricter* rather than laxer — the only direction a build-time failure may take a gate.
AUDIT_IGNORE := $(shell python3 -c 'import tomllib; print(" ".join("--ignore-vuln " + r["id"] for r in tomllib.load(open("pyproject.toml", "rb"))["tool"]["chemclaw"]["deps-audit"]["suppressions"]))')

.PHONY: deps-audit
deps-audit: ## Check the locked dependency closure for known vulnerabilities (supply chain).
	@# Against the *lockfile* rather than the environment: the exact versions `uv sync` installs
	@# here and in CI, not whatever a developer's venv has drifted to. `--no-deps` because the
	@# export is already the fully-resolved set — re-resolving would audit a different closure.
	@#
	@# **It is the closure an image installs, and for one wave this comment went on saying it was
	@# not.** Every Containerfile copies `uv.lock` and installs what `uv export --frozen` produces,
	@# with `--require-hashes`
	@# (`D-2026-09-13-an-audit-of-a-lockfile-no-image-reads-audits-nothing`) — so the paragraph that
	@# stood here, describing seven images re-resolving with pip and calling image drift
	@# **unaudited**, was falsified by the same commit that wrote the fix, and was the operator-
	@# facing copy of it. Its "11 of 100, `pandas` by a major version" was a 2026-08-28 measurement
	@# of a build form that no longer exists. Rebuilt and re-measured at that commit: 0 version
	@# differences against this export, in both directions, on every server whose image was built.
	@#
	@# What is *not* covered is named rather than implied: `servers/rxnlabel/Containerfile` installs
	@# `"rxnmapper==0.4.3" "rxn-insight==0.1.3"` straight from PyPI's CPU-torch index, which the
	@# lock does not carry. That pair is held to the lock by version rather than by hash
	@# (`tests/test_fleet.py::test_an_image_that_installs_from_the_index_pins_what_the_audit_read`),
	@# their transitive closure re-resolves, and `docs/BACKLOG.md` carries the row.
	@#
	@# **`--all-packages --all-extras` is what makes this cover anything at all, and that is a
	@# property of this workspace rather than a preference.** The root package declares
	@# `dependencies = []` and reaches every server only through its dev group, so the plain
	@# `uv export --no-dev` this pattern uses in `Chemclaw3` exports *zero* requirements here —
	@# measured: 0 pinned lines, against 50 with `--all-packages` and 136 with the extras as well. So
	@# one audit at the root does cover the whole fleet, but only in that spelling. The extras are in
	@# because an image installs them: `servers/rxnpredict/Containerfile` installs
	@# `chemclaw-mcp-rxnpredict[reaction_t5,rxn_insight]`, which is where the predictor stack — torch,
	@# transformers — actually enters a shipped closure.
	@#
	@# **`--group build` is here because that group is code that *runs*.** Every image installs it
	@# with `--require-hashes` and then builds `--no-build-isolation`, so `hatchling` and
	@# `setuptools` execute in eleven builds; a group in `uv.lock` that this export omits is
	@# `D-2026-09-13`'s own defect one group over — an audit of a closure nothing installs.
	@# Measured 2026-09-18: the flag adds 4 packages the audit had never seen (`hatchling`,
	@# `tomlkit`, `trove-classifiers`, `pathspec` — `setuptools` and `pluggy` were already in
	@# through `rxn-insight`) and moves the finding count not at all, `13 ignored` either way
	@# (`D-2026-09-18-a-backend-that-writes-the-metadata-is-a-dependency-of-the-wheel`).
	@#
	@# **A found vulnerability and an unreachable advisory database are different events, and
	@# `pip-audit` gives them the same exit code.** So the output is classified rather than the status
	@# trusted, and the answer is asymmetric on purpose. This repository's whole posture is offline —
	@# `make offline-run` takes the network away deliberately — so a developer who cannot reach the
	@# database keeps a usable gate and loses only the check that has no local answer. In CI, where
	@# the network is a given, unreachable is a **failure**: a silent skip there is a supply-chain
	@# hole that reads as a green build forever. `CI` is the signal because every runner sets it.
	@#
	@# The classified bytes are the ones the command produced, held in a variable rather than read
	@# back from a log file: a second copy of the output is a second thing that can disagree with the
	@# first. The one scratch file is an `mktemp` rather than a fixed name, because a predictable path
	@# in a shared /tmp is a symlink somebody else can plant.
	@scratch=$$(mktemp -d); trap 'rm -rf "$$scratch"' EXIT; \
	$(UV) export --all-packages --all-extras --group build --no-hashes --no-dev \
	  --format requirements-txt > "$$scratch/requirements.txt"; \
	report=$$(uvx pip-audit --no-deps --disable-pip $(AUDIT_IGNORE) \
	  -r "$$scratch/requirements.txt" 2>&1) && rc=0 || rc=$$?; \
	printf '%s\n' "$$report"; \
	if [ $$rc -ne 0 ]; then \
	  if grep -qE '$(AUDIT_FOUND)' <<<"$$report"; then exit $$rc; fi; \
	  if ! grep -qE '$(AUDIT_UNREACHABLE)' <<<"$$report"; then exit $$rc; fi; \
	  if [ -n "$${CI:-}" ]; then \
	    echo "deps-audit: the advisory database is unreachable and this is CI - the supply-chain"; \
	    echo "deps-audit: check cannot be skipped where the network is a given. Failing."; \
	    exit 1; \
	  fi; \
	  echo "deps-audit: SKIPPED - the advisory database is unreachable and CI is unset."; \
	  echo "deps-audit: the lockfile was NOT audited. Re-run with a network before you push."; \
	fi

.PHONY: run-props
run-props: ## Run the props server on its dev port with a dev token.
	CHEMCLAW_PROPS_TOKEN=$${CHEMCLAW_PROPS_TOKEN:-dev-token} \
	$(UV) run uvicorn chemclaw_mcp_props.app:app --host 127.0.0.1 --port 8850

.PHONY: run-kinetics
run-kinetics: ## Run the kinetics server on its dev port with a dev token.
	CHEMCLAW_KINETICS_TOKEN=$${CHEMCLAW_KINETICS_TOKEN:-dev-token} \
	$(UV) run uvicorn chemclaw_mcp_kinetics.app:app --host 127.0.0.1 --port 8852

.PHONY: run-unitops
run-unitops: ## Run the unitops server on its dev port with a dev token.
	CHEMCLAW_UNITOPS_TOKEN=$${CHEMCLAW_UNITOPS_TOKEN:-dev-token} \
	$(UV) run uvicorn chemclaw_mcp_unitops.app:app --host 127.0.0.1 --port 8853

.PHONY: run-suitability
run-suitability: ## Run the suitability server on its dev port with a dev token.
	CHEMCLAW_SUITABILITY_TOKEN=$${CHEMCLAW_SUITABILITY_TOKEN:-dev-token} \
	$(UV) run uvicorn chemclaw_mcp_suitability.app:app --host 127.0.0.1 --port 8892

.PHONY: run-thermalsafety
run-thermalsafety: ## Run the thermalsafety server on its dev port with a dev token.
	CHEMCLAW_THERMALSAFETY_TOKEN=$${CHEMCLAW_THERMALSAFETY_TOKEN:-dev-token} \
	$(UV) run uvicorn chemclaw_mcp_thermalsafety.app:app --host 127.0.0.1 --port 8851

.PHONY: run-chem
run-chem: ## Run the chem server on its dev port with a dev token.
	CHEMCLAW_CHEM_TOKEN=$${CHEMCLAW_CHEM_TOKEN:-dev-token} \
	$(UV) run uvicorn chemclaw_mcp_chem.app:app --host 127.0.0.1 --port 8858

.PHONY: run-safety
run-safety: ## Run the safety server on its dev port with a dev token.
	CHEMCLAW_SAFETY_TOKEN=$${CHEMCLAW_SAFETY_TOKEN:-dev-token} \
	$(UV) run uvicorn chemclaw_mcp_safety.app:app --host 127.0.0.1 --port 8859

.PHONY: run-pyexec
run-pyexec: ## Run the pyexec analysis sandbox on its dev port with a dev token.
	CHEMCLAW_PYEXEC_TOKEN=$${CHEMCLAW_PYEXEC_TOKEN:-dev-token} \
	$(UV) run uvicorn chemclaw_mcp_pyexec.app:app --host 127.0.0.1 --port 8899

.PHONY: run-calc
run-calc: ## Run the calc server on its dev port with a dev token.
	CHEMCLAW_CALC_TOKEN=$${CHEMCLAW_CALC_TOKEN:-dev-token} \
	$(UV) run uvicorn chemclaw_mcp_calc.app:app --host 127.0.0.1 --port 8860

.PHONY: run-rxnlabel
run-rxnlabel: ## Run the rxnlabel primitives on their dev port with a dev token.
	CHEMCLAW_RXNLABEL_TOKEN=$${CHEMCLAW_RXNLABEL_TOKEN:-dev-token} \
	$(UV) run uvicorn chemclaw_mcp_rxnlabel.app:app --host 127.0.0.1 --port 8865

.PHONY: run-rxnpredict
run-rxnpredict: ## Run the rxnpredict ensemble on its dev port with a dev token (needs its extras).
	CHEMCLAW_RXNPREDICT_TOKEN=$${CHEMCLAW_RXNPREDICT_TOKEN:-dev-token} \
	$(UV) run uvicorn chemclaw_mcp_rxnpredict.app:app --host 127.0.0.1 --port 8857
