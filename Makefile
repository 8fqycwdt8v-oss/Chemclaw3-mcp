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
SRC := packages/mcp_server_kit/src servers/props/src servers/chem/src servers/safety/src servers/calc/src servers/pyexec/src servers/rxnlabel/src servers/rxnpredict/src

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
type: ## mypy --strict over every server and the shared kit.
	$(UV) run mypy $(SRC)

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

.PHONY: check
check: lint type test deps-audit ## Everything CI runs bar `offline-run`, which needs `unshare`.
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
# itself a claim, so it is here, in the file that reads it, rather than in a prose document — and it
# is deliberately short: everything on it is a *deserialization of an untrusted artefact*, which is
# the one class this fleet's posture already answers, and nothing on it is reachable from a request.
# Re-derive it whenever a bump lands; an entry whose package no longer resolves to the version below
# is an entry to delete rather than to keep for safety.
#
# All six live in the optional ML extras of two servers (`rxnlabel[models]`,
# `rxnpredict[reaction_t5,rxn_insight]`) and none appears in the closure without them — measured:
# `--all-packages --no-dev` alone reports zero. They are audited anyway because those extras are
# what the *images* install.
#
#   PYSEC-2026-2447   diskcache 5.6.3, no fix released. Pickle deserialization by an attacker with
#                     write access to the cache directory. Reachable only through `rxn-utils`, in a
#                     rootless image whose model tree is mounted read-only.
#   PYSEC-2026-3447   setuptools 80.10.2, fixed in 83.0.0 — which nothing in this closure can take
#                     yet (`uv lock --upgrade-package setuptools` resolves to the same version, held
#                     by torch's own bound). The defect is in `FileList`'s MANIFEST.in matching when
#                     *building an sdist*; no server runs a build at request time or at all.
#   PYSEC-2025-217    transformers 4.57.6, no fix released. RCE via a crafted X-CLIP *checkpoint
#                     conversion*, which is a build-time script here and not shipped.
#   PYSEC-2026-2288   transformers, fixed in 5.0.0. `Trainer._load_rng_state` calls `torch.load`
#   PYSEC-2026-2289   transformers, fixed in 5.3.0. A crafted `config.json` reaching a Hub repo
#   PYSEC-2026-2290   transformers, no fix / 5.5.0. LightGlue loading honours a remote code path
#                     — all three require the library to *fetch* an attacker-controlled repository.
#                     These images bake their weights at build time, checksum them into
#                     `/opt/models/SHA256SUMS`, run with `HF_HUB_OFFLINE=1`, and sit behind a
#                     default-deny NetworkPolicy with the egress guard armed: there is no path from
#                     a request to a Hub fetch. `transformers>=5` is a major bump across a forked
#                     predictor stack, so it is a measured migration rather than a lockfile edit —
#                     the `uv` updater in `.github/dependabot.yml` is what proposes it.
AUDIT_IGNORE := \
	--ignore-vuln PYSEC-2026-2447 \
	--ignore-vuln PYSEC-2026-3447 \
	--ignore-vuln PYSEC-2025-217 \
	--ignore-vuln PYSEC-2026-2288 \
	--ignore-vuln PYSEC-2026-2289 \
	--ignore-vuln PYSEC-2026-2290

.PHONY: deps-audit
deps-audit: ## Check the locked dependency closure for known vulnerabilities (supply chain).
	@# Against the *lockfile* rather than the environment: the exact versions an image installs, not
	@# whatever is resolved in a developer's venv. `--no-deps` because the export is already the
	@# fully-resolved set — re-resolving would audit a different closure than ships.
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
	$(UV) export --all-packages --all-extras --no-hashes --no-dev --format requirements-txt \
	  > "$$scratch/requirements.txt"; \
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

.PHONY: run-chem
run-chem: ## Run the chem server on its dev port with a dev token.
	CHEMCLAW_CHEM_TOKEN=$${CHEMCLAW_CHEM_TOKEN:-dev-token} \
	$(UV) run uvicorn chemclaw_mcp_chem.app:app --host 127.0.0.1 --port 8858

.PHONY: run-safety
run-safety: ## Run the safety server on its dev port with a dev token.
	CHEMCLAW_SAFETY_TOKEN=$${CHEMCLAW_SAFETY_TOKEN:-dev-token} \
	$(UV) run uvicorn chemclaw_mcp_safety.app:app --host 127.0.0.1 --port 8859

run-pyexec: ## Run the pyexec analysis sandbox on its dev port with a dev token.
	CHEMCLAW_PYEXEC_TOKEN=$${CHEMCLAW_PYEXEC_TOKEN:-dev-token} \
	$(UV) run uvicorn chemclaw_mcp_pyexec.app:app --host 127.0.0.1 --port 8899

.PHONY: run-calc
run-calc: ## Run the calc server on its dev port with a dev token.
	CHEMCLAW_CALC_TOKEN=$${CHEMCLAW_CALC_TOKEN:-dev-token} \
	$(UV) run uvicorn chemclaw_mcp_calc.app:app --host 127.0.0.1 --port 8860
