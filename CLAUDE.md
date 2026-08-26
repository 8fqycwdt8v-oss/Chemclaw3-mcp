# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

## What this repository is

The MCP tool fleet for [`Chemclaw3`](https://github.com/8fqycwdt8v-oss/Chemclaw3), the agent for
pharmaceutical and chemical process R&D. Chemclaw3 holds the orchestration, the knowledge graph and
its own first-party capabilities; **this repository holds the tools it does not run itself**.

Chemclaw3 already has the seam for that — ADR `D-2026-08-09-a-connector-we-do-not-run`: a capability
is a directory with a `connector.yaml` declaring an `endpoint:`, the deployment supplies the address,
and no core edit is needed. So the target every server here is built against is:

> Chemclaw3 picks a new server up with **zero code changes on its side** — one directory on
> `CHEMCLAW_CONNECTORS_DIR`, one entry in `CHEMCLAW_CONNECTOR_URLS`.

## The map

| Directory | What it is |
| --- | --- |
| `servers/` | One directory per capability — a complete, independently deployable MCP server. |
| `packages/mcp_server_kit/` | The shape every server has, written once: transport, auth, identity, datasets, the egress guard. |
| `manifests/` | One directory per server holding its `connector.yaml` (a symlink). What `CHEMCLAW_CONNECTORS_DIR` points at. |
| `docs/` | How to wire this fleet to Chemclaw3, and the checklist for adding a server. |
| `scripts/` | Operational scripts outside any server's runtime — today, the offline check. |
| `tests/` | The fleet-level invariants no single server can see about itself. |
| `MODULES.md` | The catalogue and the authoritative port registry. |

**Adding a top-level directory means adding a row here and giving the directory a `README.md`** —
GitHub renders one the moment a reader clicks the folder. `tests/test_fleet.py` checks both, in both
directions, because a map nobody verifies is read, believed, and wrong.

## The rule the tree is arranged around

**One tool family = one server = one directory = one process = one port = one `connector.yaml`.**

Never bolt a tool onto an unrelated server because it is convenient. A server is a *dependency
closure* as much as a capability: `retro` carries 40+ ML engines across as many containers, and the
reason `props` starts in under a second is that it carries none of that. Merging two capabilities
merges their images, their restart blast radius and their scaling decisions.

The name is one string, used four times, and they must match: the directory under `servers/`, the
package suffix (`chemclaw_mcp_<name>`), the manifest's `name:`, and the key Chemclaw3 addresses it
by in `CHEMCLAW_CONNECTOR_URLS`.

```
servers/<name>/
├── connector.yaml                   # the manifest Chemclaw3 reads (symlinked from manifests/)
├── pyproject.toml                   # this server's dependency closure, and nobody else's
├── Containerfile                    # one rootless image per server
├── README.md                        # what it serves, what data it reads, who refreshes it
├── deploy/networkpolicy.yaml        # default-deny egress
├── src/chemclaw_mcp_<name>/
│   ├── engine/                      # pure computation — no FastAPI, no MCP, no network
│   ├── tools.py                     # the FastMCP surface; the docstrings are the prompt
│   ├── app.py                       # `app = connector_app(server, name=..., token_env=...)`
│   └── data/                        # records + dataset.json (licence, sha256, retrieved_from)
└── tests/
```

## Servers hosted in another repository

Not every server in the fleet lives here, and that is the seam working as designed rather than an
exception to it. `retro` (`chemclaw2_retrosynthesis`) is a
multi-container system with GPU profiles and its own release cadence; pulling it into this
workspace would buy nothing and cost its independence. (`rxnpredict` went the other way — a single
process with optional extras forks cleanly, and `servers/rxnpredict/README.md` records what changed.) Chemclaw3 does not care where a server is
hosted — `D-2026-08-09-a-connector-we-do-not-run` made the address the whole knob.

What such a server owes the fleet is the same contract, checked the same way:

- **A `manifests/<name>/connector.yaml` here**, with every tool classified. That is this
  repository's job, and the reason `manifests/` is a directory rather than a detail of `servers/`.
- **Bearer auth enforced on `/mcp` itself.** An external server built on `fastapi-mcp` applies its
  credential as a route dependency, and its MCP surface is *mounted* — a mount bypasses the
  enclosing app's dependencies. Verify against a running server; do not read it off the source.
- **The no-egress posture, or an argued exemption.** A gateway calling its own backend Services is
  east-west traffic and fine. A predictor calling a third-party API at request time is not — which
  is one of the reasons `chemclaw2_forward` was forked rather than adopted.
- **A row in `MODULES.md`** saying where it lives and what it costs to consume.

The one thing they cannot inherit is `mcp_server_kit`, since they are not in this workspace. That is
a reason to keep the kit's behaviours documented here in prose as well as in code — the four traps
above are properties of the MCP transport, not of this repository.

## The three layers inside a server

`engine/` ← `tools.py` ← `app.py`, and the import direction is one-way.

This mirrors Chemclaw3's own split between `science/` (the physics) and `connectors/` (the
transport), for the same reason: the computation stays testable with no transport installed, and a
FastAPI or MCP import can never creep into a correlation. `servers/props/tests/test_dataset.py` and
`test_tools.py` import no transport at all; only `test_server.py` does.

**A server is a surface, not an implementation.** If a tool starts growing real science, that
science belongs in `engine/` — and if it grows past this repository, it belongs in a library.

## The FastAPI shape, and the trap in it

Every server's `app.py` is three lines because `mcp_server_kit.connector_app` owns the shape:
`/healthz`, `/metrics`, `/mcp`, bearer auth, caller logging, a body cap, and error sanitising.
**Do not hand-roll a transport.** Four things in that helper are non-obvious, and each is quiet
when wrong:

1. **The parent app must run the MCP session manager.** `FastMCP.streamable_http_app()` returns a
   Starlette app whose *own* lifespan starts the session manager, and mounting an app does not run
   its lifespan. Miss it and the server accepts connections and then hangs on the first request —
   which reads as a network problem and is not.
2. **Route order decides what reaches `/mcp`.** The MCP app is mounted at `/` and serves `/mcp`
   itself, so `/healthz` and `/metrics` must be declared *before* the mount.
3. **The caller must be re-bound per tool call.** A tool body runs in the session manager's task,
   not the ASGI task, so middleware-bound identity is the *handshake's*. Chemclaw3 measured it:
   alice's handshake then bob's call had the tool reading alice.
4. **An unexpected exception must not reach the model verbatim.** `Tool.run` folds `str(e)` into
   the error result. `ValueError` — the family used here for deliberately worded, caller-safe
   messages — passes through; anything else is replaced and logged.

## Authentication and identity

- **Bearer on `/mcp`; `/healthz` and `/metrics` stay open.** A kubelet probe and a Prometheus
  scrape have no identity, and the exposition carries counts only.
- **Declare `auth: {mode: bearer, token_env: ...}` in every manifest, even on the loopback dev
  URL.** Chemclaw3's `HttpEndpoint` would accept `mode: none` for loopback and refuse it the moment
  a deployment moved the address — and a manifest whose auth mode changes with its address is one
  whose serving side gets it wrong. The same env-var name is read on both sides.
- **Fail closed.** A declared `token_env` whose variable is unset refuses every request. Chemclaw3
  once mounted a secret, recorded the control as enabled, and served every tool to anything that
  could reach the pod, because the serving side never checked.
- **`X-Chemclaw-Actor/Session/Correlation-Id/Dry-Run` are logged, never trusted.** Authorization
  happened in Chemclaw3 before the call was made. A server that gated on one of these headers would
  be trusting an unauthenticated string while looking like it had access control.
- **The header *names* are a contract, and `tests/test_identity_contract.py` is what holds it.**
  `HEADER_CORRELATION` read `x-chemclaw-correlation` against a sender writing
  `X-Chemclaw-Correlation-Id`: lookup is case-insensitive, not suffix-insensitive, so every server
  in this fleet bound `correlation=""` on every request from the day the header existed. It was
  invisible only because nothing consumes `current_caller().correlation` yet — the first server to
  stamp a record with it would have written an empty string into the field that joins this fleet's
  records to Chemclaw3's audit trail. That test transcribes the *sent* spellings as literals rather
  than importing this repository's constants, because the two constants agreed with each other and
  both were wrong about the sender.

## No egress. Ever. In any environment.

Every server answers from data placed on disk at build time or mounted read-only. **No server makes
an outbound call at request time.** Production is air-gapped, and this is enforced at four
independent layers because a rule that lives in one place rots:

1. **The runtime guard** (`mcp_server_kit/egress.py`), armed on import. A non-loopback
   `socket.connect` raises `EgressForbidden`. This is the layer that catches what a static scan
   cannot: a library fetching model weights, usage telemetry, a DNS-based licence check.
   `MCP_EGRESS_ALLOW` is empty by default and empty in every shipped deployment.
2. **The static scan** (`mcp_server_kit/no_egress.py`), one three-line test per server. AST-based,
   not grep-based — `import httpx as h` and `from requests import get` read differently as text and
   identically as a tree.
3. **The whole suite runs with the guard armed** (root `conftest.py`). A test that only passes by
   reaching the internet fails instead, which is what makes a vendored dataset *proven* sufficient.
   `make offline-run` goes further and takes the network away entirely.
4. **Default-deny egress at the deployment** (`servers/*/deploy/networkpolicy.yaml`), asserted by
   `tests/test_deploy.py` in both directions — `Egress` in `policyTypes` *and* an empty `egress:`.

Two consequences that decide what gets built:

- **A module whose value depends on a live third-party API is rejected, or redesigned around a
  snapshot.** ADMETlab 3.0 is a hosted API, so `admet` uses local open models instead; EPO OPS is a
  live API, so `patents` uses the SureChEMBL bulk snapshot. See `MODULES.md`.
- **"Mirror" always means a build-time snapshot or a mounted export**, never a request-time call to
  somebody else's host. Refreshing one is a build step or an operator-run script *outside* the
  serving image, reviewed by a person in a pull request.

## Vendored data

Every corpus ships with a `dataset.json` carrying `name`, `version`, `licence`, `retrieved_from`,
`description` and `sha256`. All six are required and `load_dataset` refuses without them: a corpus
with no recorded licence is a legal question nobody can answer a year later, one with no checksum
cannot be shown to be what the review approved, and `retrieved_from` is the only record of where a
human obtained the file. **Nothing reads `retrieved_from` as an address**; the guard would refuse.

**Validate the corpus against itself.** A hand-compiled table is a table with typos in it, and the
realistic failure is not a bad decision but a transposed digit in a row nobody looks at again.
`servers/props/tests/test_dataset.py` is the pattern worth copying: CAS check digits, molecular
weight against formula, and Antoine constants against the tabulated boiling point are all
*independently written* numbers that must agree. Each one catches a typo without consulting
anything external.

## The manifest is a contract, and it is checked

`connector.yaml` declares the tool surface Chemclaw3 will advertise. Three rules, all enforced by
`mcp_server_kit.testing.assert_manifest_matches` against a **running** server:

- Every served tool is declared. An undeclared tool is reachable by anything that can open a socket
  to the pod while looking, in review, like it does not exist.
- Every declared tool is served, or Chemclaw3 advertises a capability that fails at call time.
- Every tool is classified exactly once as `read_only` or `state_changing` — the same rule
  Chemclaw3's `HttpEndpoint` enforces (D-167). Getting it wrong by omission fails *open*: the plan
  gate would let an unapproved plan call a state-changing tool.

Chemclaw3's own lesson applies directly here: **a README is not a gate.** Its `mcp_servers/calc/`
was asserted deleted across four ADRs while still tracked, still built into the image, and still
dispatchable. Anything this file claims about a server should be checked by a test in that server.

## Tool docstrings are the prompt

Argument names, defaults and docstring prose are what the agent reads before deciding whether to
call a tool and what to pass it. Write them for a chemist:

- **State the units.** Every one.
- **State what the tool is not.** A vapour pressure from a Trouton estimate is not VLE data; a
  Hansen shortlist is not a recommendation and knows nothing about reactivity. A docstring that
  omits this gets the tool used outside its range, and the number reaches a chemist unwarned.
- **Return provenance with the answer.** Every result carries `source`; a property without its
  provenance is not something anybody can put in a report.
- **Return the method when there is more than one.** `props` returns `method` and `caveat` beside
  every vapour pressure precisely because the two routes are not equally good.
- **Refuse rather than approximate.** An unknown solvent is an error naming the corpus, not the
  nearest match. A silently substituted input corrupts everything downstream of it.

## Cost, and where a slow tool belongs

A tool call is inside a conversation turn, so most of this fleet is milliseconds: `props` is a dict
lookup and a bisection, `chem` draws an SVG.

**The rule is not a duration, though, and stating it as one was a mistake this repository made and
corrected.** `servers/calc` runs geometry optimisations that take a minute and CREST searches that
take hours. What the fleet promises is **statelessness**: a server takes its arguments, computes,
and returns. No job record, no resumption, no progress channel — if a call is interrupted the caller
calls again, and durability belongs to Temporal on the Chemclaw3 side.

So the question for a new capability is not "how long", it is **request/response or orchestration**.
A composite — optimise, take a Hessian, displace along the imaginary mode, repeat — is a loop with
state, and the structural giveaway is that *its key names its own output*, so nobody can ask "have I
computed this already?" before running it. That belongs in Chemclaw3 as a durable job. Its **parts**
belong here, each separately keyed, and the caller composes them. `servers/calc` is the worked
example: it exposes `relax_structure`, `compute_hessian`, `scan_point` and the CREST searches as
primitives, and Chemclaw3's activities assemble thermochemistry, scan profiles, reaction energetics
and interaction energies out of them — caching one row per primitive instead of one per job.

What a slow tool still owes the fleet: a bound on its input so the cost cannot run away unpriced, a
`request_timeout` stating the real budget, a docstring that tells the model what it is asking for,
and a `calculation_key`-style probe if the caller is expected to cache it. See
`docs/adding-a-server.md`.

**That bound belongs in the engine, not in the transport, and the reason is measurable.** A
per-tool-call wall clock in `connector_app` looks like the general answer and is not: every heavy
tool here offloads with `asyncio.to_thread`, and cancelling the awaiting coroutine does not stop the
worker thread — so such a timeout would return an error to a caller who has already gone while the
CPU burn continued, which is a control that reads as one and is not. What actually stops the cost is
`xtb_cli.run_isolated`: the run gets its own process group (`start_new_session=True`) and the whole
group is killed on timeout, because `xtb` forks workers that `subprocess.run(timeout=…)` leaves as
orphans still burning CPU and still writing into a tempdir the caller has already removed.
`servers/calc/tests/test_process_isolation.py` pins both directions — that the isolated form reaches
the fork, and that the naive form does not, which is what makes the first assertion mean something.
Chemclaw3 cancels from its side too (`D-2026-08-26-a-request-timeout-bounds-the-wait-not-the-work`);
this is the half that holds when the caller vanishes without saying anything.

## Ports

| Port | Server | Status |
| --- | --- | --- |
| 8850 | `props` | built |
| 8857 | `rxnpredict` | built (fork of `chemclaw2_forward`) |
| 8858 | `chem` | built (port of Chemclaw3's own `chem` bundle — see below) |
| 8859 | `safety` | built (port of Chemclaw3's own `safety` bundle — see below) |
| 8860 | `calc` | built (the physics behind Chemclaw3's `calc` bundle, as keyed primitives — a **backend**, not a connector; see below) |
| 8851–8856 | `thermalsafety`, `kinetics`, `unitops`, `rxnsearch`, `blocks` | proposed |
| 8854 | `retro` | **hosted in the chemclaw2 repository** — adopted, not rebuilt |
| 8861+ | compound identity & data | proposed |
| 8870+ | safety, tox & regulatory | proposed |
| 8880+ | literature & IP | proposed |
| 8890+ | spectra & analytics | proposed |

The 8850+ block is deliberate: Chemclaw3's own connectors sit at 8810–8815 and `Chemclaw3_mock` at
8090–8091, so nothing here can collide with a local full-stack run. `MODULES.md` holds the
authoritative per-server assignment; claim the next free port there in the same pull request that
adds the server.

## Never duplicate a Chemclaw3 capability

Chemclaw3 already serves these, and a second implementation would be a second answer to one
question — the failure its own `connectors/README.md` records as two live definitions of
`predict_pka` differing in one of them:

| Already in Chemclaw3 | Where |
| --- | --- |
| xTB energies, pKa, logD, solubility, thermochemistry — **the tools and the orchestration stay Chemclaw3's; the physics underneath moved.** The calibration ledger, the calculation cache, the artifact store and every durable calc job are wholly Chemclaw3's | `calc`, computing through `servers/calc/`; see below |
| Bayesian optimisation, screening designs, campaign progress | `bo` |
| ECFP4/DRFP similarity and substructure search | `molfp`, `rxnfp` |
| ~~Structural hazard alerts, genotoxic alerts, ICH Q3C/Q3D impurity limits~~ | now `servers/safety/` |
| DFT via Nextflow/HPC | `qm` |
| Knowledge graph read/write, the PR-gate | core |
| ELN and ORD ingestion | `ingest/sources` |

Before proposing a tool, check `MODULES.md` and the table above. Overlap that is deliberate must be
argued in the server's README — `rxnsearch` is scoped to *aggregate condition statistics* precisely
because per-record ORD retrieval is already `eln-ord` plus `rxnfp`.

**Two rows of that table have left it outright: `chem` is now `servers/chem/` and `safety` is now
`servers/safety/`.** A capability moving out of Chemclaw3 is the one sanctioned way off this list,
and it is only sanctioned when the move is a *replacement*: same manifest `name`, same tools, same
arguments, so exactly one of the two can be addressed (`CHEMCLAW_CONNECTOR_URLS` is keyed by name,
and `CHEMCLAW_CONNECTORS_DIR` gives the first directory the collision). A port that renamed itself
would leave both live, which is the duplication this section exists to prevent. See
`servers/chem/README.md` and `servers/safety/README.md`.

**`calc` is the third row and it left in a different way, which is why it is struck through only in
part.** Chemclaw3 keeps its `calc` bundle and all fifteen of its tools; what moved is the *physics
underneath* — exposed here as `servers/calc/`, a **backend** Chemclaw3 calls from inside
`science/calc/store.py::cached_compute` on a cache miss, not a connector it dials. Putting this
fleet's `manifests/` on `CHEMCLAW_CONNECTORS_DIR` would let a partial surface win the name collision
and remove the calibration ledger, the calculation cache, the artifact store and every durable job
from the agent's surface, with no error.

`cached_compute` takes the key as an *argument*, so a key that only arrives on the result would be
unusable there — which is why that server serves `calculation_key`, returning the identity of a
calculation before it runs. That is what makes the split honest: Chemclaw3 never derives a key, so
the only thing the two repositories must keep in step is the value of `CALCULATION_EPOCH`.

**And it is why that server ships primitives rather than composites.** A calculation whose key names
its own *output* is a loop with state, and a loop with state is a durable job — so
`compute_thermochemistry` is not there, while `relax_structure`, `compute_hessian`, `scan_point` and
the two CREST searches are, each separately keyed. Chemclaw3's activities assemble thermochemistry,
scan profiles, reaction energetics and interaction energies from them, and cache one row per
primitive instead of one per job. See `servers/calc/README.md` and `docs/integration.md`.

## Working in this repository

```sh
make install         # uv sync
make check           # lint + mypy --strict + the whole suite (what CI runs)
make offline-run     # the same suite with the network namespace taken away
make run-props       # the reference server on 127.0.0.1:8850
make run-safety      # one per server; see the Makefile for the full list
make run-calc        # the heaviest one — a call here can be minutes or hours, deliberately
```

- Python ≥ 3.11, `uv` workspace, `ruff` (line length 100), `mypy --strict`.
- The `mcp` SDK is pinned to the **1.x line** deliberately: Chemclaw3 is on `mcp.server.fastmcp`,
  and matching its generation keeps `connector_app` line-for-line comparable with
  `chemclaw.connectors.server`. Moving to 2.x (`MCPServer`) is a deliberate migration for both
  repositories, not a lockfile bump.
- Adding a top-level directory means adding a row to this file and giving the directory a
  `README.md` — GitHub renders one the moment a reader clicks the folder.
- A change to a server's tool surface is a change to its `connector.yaml` in the same commit. The
  test that checks them will fail otherwise, which is the intent.
