# CLAUDE.md

Current rules only; the *why* is in [`docs/decisions/CURRENT.md`](docs/decisions/CURRENT.md).

## What this repository is

The MCP tool fleet for [`Chemclaw3`](https://github.com/8fqycwdt8v-oss/Chemclaw3): the tools that
agent does not run itself. Chemclaw3 picks a new server up with **zero code changes on its side** —
one directory on `CHEMCLAW_CONNECTORS_DIR`, one entry in `CHEMCLAW_CONNECTOR_URLS`.

## The map

| Directory | What it is |
| --- | --- |
| `servers/` | One directory per capability — a complete, independently deployable MCP server. |
| `packages/mcp_server_kit/` | The shape every server has, written once: transport, auth, identity, tracing, datasets, limits, the egress guard. |
| `packages/chemclaw_contracts/` | The one owner of every `connector.yaml` (as package data) and of the typed wire of the `calc` and `rxnlabel` backends; Chemclaw3 takes it as a pinned dependency. |
| `manifests/` | One symlinked `connector.yaml` per **connector** — what `CHEMCLAW_CONNECTORS_DIR` points at. |
| `manifests-internal/` | The servers Chemclaw3 must **not** discover (`calc`, `rxnlabel`); each declares `mount: backend`, a key Chemclaw3's manifest model refuses. |
| `docs/` | Operating, integrating and adding servers; the decision record (`docs/decisions/`) and the open queue (`docs/BACKLOG.md`). |
| `scripts/` | Operational scripts outside any server's runtime, listed in `scripts/README.md`. |
| `tests/` | Fleet-level invariants no single server can see about itself. |
| `MODULES.md` | The catalogue and the only port registry. |

A new top-level directory gets a row here and a `README.md` (a fleet test checks both directions).

## One tool family = one server

**One tool family = one server = one directory = one process = one port = one `connector.yaml`.**
Never bolt a tool onto an unrelated server: a server is a dependency closure, a restart blast radius
and a scaling decision. The name is one string used four times — the directory under `servers/`, the
package suffix (`chemclaw_mcp_<name>`), the manifest's `name:`, and the `CHEMCLAW_CONNECTOR_URLS`
key. Required files: [`docs/adding-a-server.md`](docs/adding-a-server.md#the-files).

A server hosted elsewhere (e.g. `retro`) owes the same contract: a classified manifest here, bearer
on `/mcp` itself (verified live), no egress, `/mcp` answering its Service name, a `MODULES.md` row.

## Inside a server

`engine/` ← `tools.py` ← `app.py`, one-way. The science lives in `engine/` and is testable with no
transport installed; `tools.py` is the MCP surface; `app.py` is three lines over
`mcp_server_kit.connector_app`. **Do not hand-roll a transport and do not call `basicConfig`.**
`connector_app` owns the traps, each quiet when wrong:

1. The parent app runs the MCP session manager's lifespan (a mount does not), or the first call hangs.
2. `/healthz`, `/livez` and `/metrics` are declared before the `/` mount, or `/mcp` swallows them.
3. The caller identity and trace context are re-bound per tool call, not taken from the handshake.
4. A non-`ValueError` exception never reaches the model verbatim: it is replaced and logged with one
   `error_id` in both halves; a `ValueError` passes through redacted.
5. `configure_logging()` forces (`FastMCP` calls `basicConfig` at import) and runs from the lifespan.
6. Upstream's DNS-rebinding guard stays on; `MCP_ALLOWED_HOSTS` adds the Service's `name:port`.

## Authentication, identity and health

- Bearer on `/mcp`; `/healthz`, `/livez`, `/metrics` are open and carry no actor, session or
  argument label (a tool name only clamped to the served surface).
- Every manifest declares `auth: {mode: bearer, token_env: ...}`, even on loopback; an unset
  `token_env` refuses every request. Each server's `tests/test_server.py` drives
  `assert_bearer_is_enforced` and `assert_manifest_matches` against its running listener.
- `X-Chemclaw-Actor/Session/Correlation-Id/Dry-Run` are logged, never trusted; authorization happened
  in Chemclaw3. The header spellings are a contract held by `tests/test_identity_contract.py`.
- `/healthz` is readiness: it runs the thing on a fixture, answers 503 naming a permanent cause
  (`degradation.PERMANENT_CAUSES`), 200 with `degraded` for a transient one, and reports `bounds`.
  Corpora load lazily, so a bad corpus is the probe's answer, not an import error. Unready means a
  capability is gone, never that an optional set shrank. `/livez` consults nothing.

## No egress, in any environment

Data is on disk at build time or mounted read-only. **No outbound call at request time.** Four layers:

1. **Runtime guard** (`mcp_server_kit/egress.py`, armed on import): refuses non-loopback connects,
   sends and lookups (`arm()` is the list), logs, counts on `chemclaw_mcp_egress_refused_total`,
   raises `EgressForbidden`; a path answering with a component missing classifies it through
   `degradation.py`. Outside it: child processes, `ctypes`, `_socket.socket`, compiled extensions.
   `MCP_EGRESS_ALLOW` is empty in every shipped deployment; the `chemclaw_mcp_egress_guard_armed`
   gauge makes a deployment with `MCP_EGRESS_GUARD=off` visible from a scrape.
2. **Static scan** (`mcp_server_kit/no_egress.py`), AST-based, one test per server. A computed
   `import_module(name)` is an offence until that server's test justifies it at its call site.
3. **The suite runs with the guard armed** (root `conftest.py`); `make offline-run` removes the
   network namespace and is the only cover for child processes and `ctypes`. `make check` runs it
   where the kernel allows an unprivileged network namespace and names it where it cannot.
4. **Default-deny egress NetworkPolicy** per server, held fleet-wide by `tests/test_deploy_shape.py`.

A capability that needs a live third-party API is rejected or rebuilt around a build-time snapshot
or mounted export, refreshed outside the serving image. Tracing constructs no exporter.

## Data and manifests

Every corpus ships a `dataset.json` with `name`, `version`, `licence`, `retrieved_from`,
`description`, `sha256`, `refresh_owner` and `refresh_cadence`; `load_dataset` refuses without them.
Validate a hand-compiled corpus against itself (see `servers/props/tests/test_dataset.py`).

`connector.yaml` is the surface Chemclaw3 advertises: every served tool declared, every declared tool
served, each classified once as `read_only` or `state_changing` — changed in the same commit. It
lives in `packages/chemclaw_contracts` (the server's and `manifests/`'s paths are links) and carries
`contract_version`, bumped by the rules in `docs/adding-a-server.md`; `/healthz` reports the same.

**Tool docstrings are the prompt.** State the units and what the tool is **not**; return `source`
with every answer and `method` when there is more than one; refuse rather than approximate.

## Cost and statelessness

A server is **stateless request/response**: no job record, no resumption, no progress channel. A
loop with state (a composite whose key names its own output) is a durable job in Chemclaw3; its
parts ship here as separately keyed primitives (`servers/calc` is the example). A slow tool owes: a
bound on its input, a `request_timeout` stating the real budget, a docstring saying what it costs, a
`calculation_key`-style probe if callers cache it, and an admission ceiling that counts what the pod
spends (threads, not calls) and refuses promptly when full. Bounds live in the engine; a subprocess
runs in its own process group, killed whole on timeout. The kit bounds sessions
(`MCP_MAX_SESSIONS`). Whether a tool is admission-gated does not follow the manifest's
`read_only`/`state_changing` split. **Ports:** `MODULES.md` is the only registry; claim the next
free port in **8850–8899** there, in the PR that adds the server.

## Never duplicate a Chemclaw3 capability

| Already in Chemclaw3 | Where |
| --- | --- |
| xTB tool surface, calibration ledger, calculation cache, durable calc jobs (physics: `servers/calc`) | `calc` |
| Bayesian optimisation, screening designs | `bo` |
| ECFP4/DRFP similarity and substructure search | `molfp`, `rxnfp` |
| Knowledge graph read/write | core |
| ELN and ORD ingestion | `ingest/sources` |
| Publishing results to an external store | `results` |

No DFT tier: no tool shelling out to ORCA/Psi4/Gaussian/NWChem or calling a hosted QM API. A
capability leaves Chemclaw3 only as a *replacement* (same `name`, tools, arguments). Deliberate
overlap is argued in the server's README. The two `CALCULATION_EPOCH` constants compose
(`remote_key` folds Chemclaw3's over `calc`'s `params_hash`): a bump on either side invalidates all.

## The record and the queue

- `docs/decisions/` — one `D-YYYY-MM-DD-<slug>.md` per choice or decline (a defect fix is a commit
  and a test), from [`TEMPLATE.md`](docs/decisions/TEMPLATE.md), row in its README; never edited
  except to add `Superseded-by:`; ends with `## What keeps it true` naming its tests.
- `docs/BACKLOG.md` — a queue, not a log: a closed row is deleted in the commit that closes it; every
  row names an anchor in the tree. No document here states a count of itself.

## Working here

```sh
make check                  # lint + mypy --strict + suite with coverage + offline lane + audit
make architecture-baseline  # import/handshake latency and prose share, to docs/
make run-<server>           # one server on its manifest's port with a dev token
```

- Python ≥ 3.11, `uv` workspace (`make install`), `ruff` (line 100; `S`, `ASYNC`, `BLE`),
  `mypy --strict` incl. tests. A blind `except Exception` that answers anyway classifies through
  `degradation`, carries the `# noqa: BLE001` ruff asked for, or is argued in the fleet test's
  allowlist. `RUF100` is selected, so a `noqa` ruff did not ask for is itself an error.
- **No `assert` in serving code** (`python -O` deletes it); use `if ...: raise`.
- Images install `uv export --frozen` with `--require-hashes`; publishing needs the gate on that
  revision (Jenkins `Preflight`). The `mcp` SDK stays on 1.x, matching Chemclaw3.
- Docstrings: what, why, invariants; ≤~10 lines; no history and no corrections of earlier prose.
