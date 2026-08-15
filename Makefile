# The gate. `make check` is what CI runs; nothing here needs a cluster, a database or a network.
.DEFAULT_GOAL := help
UV ?= uv
SRC := packages/mcp_server_kit/src servers/props/src servers/chem/src

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
	unshare --net -- $(UV) run python scripts/offline_check.py -q

.PHONY: check
check: lint type test ## Everything CI runs.

.PHONY: run-props
run-props: ## Run the props server on its dev port with a dev token.
	CHEMCLAW_PROPS_TOKEN=$${CHEMCLAW_PROPS_TOKEN:-dev-token} \
	$(UV) run uvicorn chemclaw_mcp_props.app:app --host 127.0.0.1 --port 8850

.PHONY: run-chem
run-chem: ## Run the chem server on its dev port with a dev token.
	CHEMCLAW_CHEM_TOKEN=$${CHEMCLAW_CHEM_TOKEN:-dev-token} \
	$(UV) run uvicorn chemclaw_mcp_chem.app:app --host 127.0.0.1 --port 8858
