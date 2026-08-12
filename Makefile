# The gate. `make check` is what CI runs; nothing here needs a cluster, a database or a network.
.DEFAULT_GOAL := help
UV ?= uv
# Globbed, not listed. The listed version silently stopped covering `rxnpredict` the day that
# server landed: `make check` stayed green over 15 files while 28 more went unchecked, which is
# exactly the failure a hand-maintained list of directories produces. `tests/test_fleet.py` asserts
# that every server under `servers/` is reached by this.
SRC := packages/mcp_server_kit/src $(wildcard servers/*/src)

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

.PHONY: run-rxnpredict
run-rxnpredict: ## Run the rxnpredict server on its dev port. With no extras installed it serves
                ## its tools and reports every predictor unavailable — see its README for a double.
	CHEMCLAW_RXNPREDICT_TOKEN=$${CHEMCLAW_RXNPREDICT_TOKEN:-dev-token} \
	$(UV) run uvicorn chemclaw_mcp_rxnpredict.app:app --host 127.0.0.1 --port 8857
