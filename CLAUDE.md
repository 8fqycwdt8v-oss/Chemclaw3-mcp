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
| `packages/mcp_server_kit/` | The shape every server has, written once: transport, auth, identity, trace continuation, datasets, the egress guard. |
| `manifests/` | One directory per **connector** holding its `connector.yaml` (a symlink). What `CHEMCLAW_CONNECTORS_DIR` points at, and only what may safely go there. |
| `manifests-internal/` | The same, for the servers Chemclaw3 must **not** discover — `calc` (a backend behind `cached_compute`) and `rxnlabel` (a background drain's primitives). No published `export` line names it, and each manifest here declares `mount: backend`, a key Chemclaw3's `extra="forbid"` manifest model refuses. |
| `docs/` | How to wire this fleet to Chemclaw3, the checklist for adding a server, the decision record (`docs/decisions/`) and the open queue (`docs/BACKLOG.md`). |
| `scripts/` | Operational scripts outside any server's runtime; `scripts/README.md` lists them, and a test reads that list against the directory. |
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

**What a server ships is declared once, in [`docs/adding-a-server.md`](docs/adding-a-server.md#the-files)**,
and `tests/test_fleet.py::test_a_server_ships_the_whole_set` is what requires it. A copy of that
tree used to stand here and had drifted to well under what the test demands — the Deployment, the
HPA, the PDB and every one of the `tests/` files were missing from it — so a reader who copied this
file's version failed their first `make check` and could not tell which of the two documents was
wrong. That is the deleted port table two sections down, with filenames instead of numbers: a second
declaration nothing reconciles is read, believed and stale.
`tests/test_fleet.py::test_the_required_file_set_is_declared_once` now holds the checklist against
the requirement, and holds this file to not growing a second copy.

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
`/healthz`, `/metrics`, `/mcp`, bearer auth, caller logging, a body cap, error sanitising, **the
process's log configuration and the per-tool metrics**. The last two are there for the same reason
as the rest: an observability decision taken one server at a time is taken in some of them, and
before it moved here the fleet had no owned log configuration anywhere and no application metric at
all. **Do not hand-roll a transport, and do not call `basicConfig` in a server.** Five things in
that helper are non-obvious, and each is quiet when wrong:

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
   messages — passes through; anything else is replaced and logged, with a short `error_id` in
   both halves so the notice the model quotes and the traceback an operator greps are one fault.
5. **`configure_logging()` has to force.** `FastMCP.__init__` calls `basicConfig` at import of the
   server's `tools.py`, long before `app.py` runs, so anything that does not pass `force=True`
   loses to it silently — and the fleet keeps upstream's `"%(message)s"`: no timestamp, no level,
   no logger name, with a WARNING and an INFO byte-identical.

## Authentication and identity

- **Bearer on `/mcp`; `/healthz` and `/metrics` stay open.** A kubelet probe and a Prometheus
  scrape have no identity. The exposition is the default registry's — `python_info` and the
  `process_*` collectors — **plus this fleet's own per-tool counters and latencies**
  (`packages/mcp_server_kit/src/mcp_server_kit/metrics.py`), and it carries nothing about a
  *caller*: no actor, no session, no correlation id, no tool argument. It said "counts only" for as
  long as it published the interpreter version, and the test that covered the endpoint asserted the
  non-count was there.
  A labelled metric on this endpoint must never take an actor, a session or a tool argument as a
  label; a tool **name** is none of those and is allowed, on the condition that it is clamped to
  the served surface — the name in a `tools/call` is caller-supplied, so an unclamped label mints
  a series per string anything that can reach the pod invents. A destination host is not clampable
  and is therefore not a label at all (`chemclaw_mcp_egress_refused_total` is bare).
  `packages/mcp_server_kit/tests/test_connector_app.py` asserts all of that over the live
  exposition, in both directions: the forbidden words are absent **and** the metrics are there.
- **`/healthz` is readiness, not a constant 200.** A server whose corpus, rule table or backend
  will not load answers 503 naming the reason, and lists what it did verify as `name@version`.
  Datasets here load lazily, so before this a `chem` pod with a corpus that failed its checksum
  passed the probe, took traffic and failed every call. A new server passes `readiness=` to
  `connector_app`; see `docs/adding-a-server.md`.
  **And it loads its corpus lazily, so the probe is what fails rather than the import**
  (`D-2026-09-18-a-corpus-that-cannot-be-read-is-a-probe-s-answer-not-an-import-error`). Two servers
  touched theirs at module scope — `props` deriving a tool-schema bound from the table's size,
  `rxnpredict` through a settings load — and verifying a corpus in an import means failing in one:
  driven, `DatasetError` out of `import <pkg>.tools`, no listener, and the two hashes reaching an
  operator as `CrashLoopBackOff` plus a container log rather than as a 503 body.
  `tests/test_fleet.py::test_a_corrupt_corpus_is_the_probe_s_answer_rather_than_an_import_error`
  holds it for every server that vendors a corpus, derived from the corpora on disk.
  **Passing one is not the same as the check working, and when it was measured several of the
  fleet's probes passed with a real dependency broken**
  (`D-2026-09-12-a-readiness-check-that-does-not-run-the-thing-is-not-a-readiness-check`): a probe
  that checks a component *constructed*, or that a version string could be *derived*, passes a
  component that builds and then fails on every call — so a probe runs the thing, on a fixture, and
  a new one proves it by breaking a dependency and reading the status. The other half is what a
  probe must **not** act on: only `mcp_server_kit.degradation.PERMANENT_CAUSES` may produce an
  unready answer, and a transient resource exhaustion is counted and left alone. **That rule is
  `connector_app`'s, not each callable's** — stated per callable it was read in exactly two places
  across the whole fleet, so every other raise went out as an unconditional 503
  (`D-2026-09-13-a-probe-that-can-kill-the-pod-is-not-a-readiness-probe`). A probe failure whose
  cause is transient answers **200** with `degraded` naming it.
- **`/livez` is liveness, and it is a different route because the two answers differ.** A readiness
  503 takes the pod out of its Service and is undone by the next passing probe; a liveness failure
  kills the container. Every Deployment here pointed both at `/healthz` — `periodSeconds: 30`,
  `failureThreshold: 3` — so a broken *optional* component was a kill after ~90 s: driven, one
  missing checkpoint among eleven optional `rxnpredict` predictors answered 503, and since a restart
  cannot recreate a missing file the pod that had served ten of eleven served none. `/livez` consults
  nothing and proves only that the process still serves HTTP, which is the fault a restart fixes.
  `tests/test_deploy_shape.py::test_liveness_and_readiness_do_not_share_a_route` holds every
  Deployment to both paths and to their inequality.
- **What makes a pod unready is a capability it cannot deliver, never the size of an optional set.**
  A `rxnpredict` pod serving ten of eleven predictors is serving; it refuses only when a whole kind
  of prediction is gone *and* something broke to take it, or when an `ENABLED_*_MODELS` allow-list
  names a predictor that is not registered — somebody wrote the name down. The same rule makes
  `calc` ready on an image with no `xtb` binary while `compute_atomic_descriptors` and
  `compute_surface_potential` refuse by name, at the point of *asking* as well as of computing: a
  well-formed key naming a program the pod lacks is worse than no key, and `calculation_key` was
  minting one for two of its tools under the shipped default.
- **Declare `auth: {mode: bearer, token_env: ...}` in every manifest, even on the loopback dev
  URL.** Chemclaw3's `HttpEndpoint` would accept `mode: none` for loopback and refuse it the moment
  a deployment moved the address — and a manifest whose auth mode changes with its address is one
  whose serving side gets it wrong. The same env-var name is read on both sides.
- **Fail closed.** A declared `token_env` whose variable is unset refuses every request. Chemclaw3
  once mounted a secret, recorded the control as enabled, and served every tool to anything that
  could reach the pod, because the serving side never checked.
- **Every server proves that against its own running listener, and `connector_app` being shared is
  not the reason it need not.** "The helper enforces it, so every server enforces it" is precisely
  the inference a mount bypass defeats — the credential can be declared, reviewed and applied to
  everything except the mounted route. So `mcp_server_kit.testing.assert_bearer_is_enforced` drives
  each server's real app under uvicorn on loopback through the anonymous caller, a wrong token, the
  right secret under the wrong scheme, the credential actually serving, and the declared variable
  unset; the manifest is what it reads the variable's *name* from, so the serving side is held to
  what Chemclaw3 was told to send.
  `tests/test_fleet.py::test_every_server_proves_its_bearer_check_against_a_running_server` is what
  makes an eighth server owe the same proof. What that lane does **not** prove is what an image
  does: it runs this repository's `app` object under this repository's uvicorn, so a Containerfile
  that starts a different entrypoint, or an ingress in front of the pod, is outside it.
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
   `connect`, a datagram send, or a forward or reverse name lookup is logged at ERROR with the
   host, counted on `chemclaw_mcp_egress_refused_total`, and raises `EgressForbidden`. **Exactly
   which calls is `arm()`'s set of rebindings, and is not restated here**: this sentence enumerated
   six while the guard patched nine, and the three it never mentioned — the reverse-lookup pair and
   the second forward one — were added to the module without being carried back into the prose.
   That is the deleted port table's defect with a function name instead of a number, so the module
   is the declaration and
   `tests/test_fleet.py::test_claude_md_claims_no_interception_the_guard_does_not_make` is what
   stops a re-enumeration drifting again. **The log and the counter are
   load-bearing rather than decorative**: `EgressForbidden` subclasses `OSError`, so what a refusal
   looks like from outside depends on who catches it — `calc` reports it as "could not resolve the
   xTB backend", and any library's own `except OSError: retry` swallows it whole. `rxnpredict` used
   to gather it into a *silently* degraded ensemble and no longer does: every path that **answers
   with a component's contribution missing** classifies the exception through
   `mcp_server_kit/degradation.py`, which tests `EgressForbidden` before anything else precisely
   because the inheritance would otherwise sort a refusal beside a connection reset, and counts it
   on `chemclaw_mcp_degraded_total{server,component,cause}`. The call sites and their causes are
   held by `packages/mcp_server_kit/tests/test_degradation.py::
   test_every_call_site_derives_its_cause_rather_than_writing_one`, as a count, because a collected
   list that must come back empty is satisfied by the calls being gone.
   **This sentence used to say "every path in this fleet that catches an exception and answers
   anyway", and that is a different and false claim**: a serving path can catch and answer with
   something other than a missing contribution. `rxnlabel/engine/mapping.py`'s `inference_threads`
   charges the admission ceiling `1` when torch cannot be asked its width, which under-charges
   rather than degrades; nothing this fleet answered without is missing, so counting it on
   `chemclaw_mcp_degraded_total` would publish a series about nothing.
   **The second example this paragraph gave has been deleted and the paragraph outlived it**:
   `rxnpredict/engine/cache.py` was said to fall back to the *uncanonicalised* SMILES as a cache
   key, and `D-2026-09-13-a-cache-key-derived-from-text-nobody-validated-is-not-a-key` removed
   exactly that fallback — `_canonical_or_none` returns `None` and the entry is not cached, the
   `ValueError` arm is deliberately silent because a caller's typo is not this pod degrading, and
   the blind arm classifies. A prose example naming a deleted branch reads as a live exemption
   (`D-2026-09-14-a-ratchet-that-matches-a-comment-holds-nothing`, on the same wave's other
   sentences).
   **What holds the claim rather than asserting it** is
   `tests/test_fleet.py::test_every_blind_handler_that_answers_anyway_is_argued`, which is where an
   exemption now has to be written for a reader to believe there is one
   (`D-2026-09-14-a-lint-rule-that-does-not-fire-is-not-the-control-it-was-read-as`).
   `chemclaw_mcp_egress_guard_armed` is what makes a deployment that shipped `MCP_EGRESS_GUARD=off`
   visible from a scrape rather than from a docstring. This is the layer that catches what a
   static scan cannot: a library fetching model weights, usage telemetry, a DNS-based licence check.
   `MCP_EGRESS_ALLOW` is empty by default and empty in every shipped deployment.

   **`mcp_server_kit/tracing.py` is inside this rule, not an exception to it.** It continues the
   W3C trace context Chemclaw3 sends, so a tool call is a span under the turn that asked for it —
   and it constructs no provider, no processor and no exporter. Without a configured SDK the tracer
   is a proxy producing non-recording spans and no I/O at all, `MCP_TRACING_ENABLED` is off by
   default, and `opentelemetry-api` is an extra rather than a fleet-wide dependency. Exporting
   those spans means a deployment installing an SDK and opening a destination, which is a decision
   to argue for in exactly the way `MCP_EGRESS_ALLOW` is.

   **It covered only `connect` for a while, and the docstring named DNS anyway** — so two of the
   three examples above walked past it, and a `bytes` host in the address tuple walked past it in
   pure Python. What is still outside it *by construction* is now stated rather than implied, and it
   is **four channels**: a **child process**, a **`ctypes` call into `libc`**, the private C type
   **`_socket.socket`** (`arm()` rebinds the methods of the Python `socket.socket` subclass, never
   the C type it inherits from), and any syscall from a **compiled extension**.

   Which of the other layers reaches which is worth getting right, because this paragraph had it
   wrong in three directions. It counted `grpc` as one of the four; it said layer 2 sees two of
   them; and it called `grpc` "the **instance** of the fourth that this lockfile actually reaches —
   measured opening a real connection to a non-loopback address", which **cannot be re-run in this
   workspace or in any image it builds**. `grpcio` is in `uv.lock` only as a transitive dependency of
   `tensorboard`, behind an optional extra, and it is absent from `uv export --frozen`'s output —
   which is what every `Containerfile` installs with `--require-hashes`
   (`D-2026-09-13-an-audit-of-a-lockfile-no-image-reads-audits-nothing`). So `import grpc` fails in
   the dev venv, and the compiled-extension channel has **no installed instance anywhere in this
   fleet**: it is outside the guard *by construction* and named from that, never from a measurement.
   The three channels that *are* open were driven with the guard armed — a **child process**, a
   **`ctypes` call into `libc`** and the private **`_socket.socket`** C type each completed with
   `chemclaw_mcp_egress_refused_total` flat, against a python `socket.socket`, a `getaddrinfo` and a
   UDP `sendto` that were refused and counted. `tests/test_fleet.py::
   test_claude_md_does_not_claim_a_measurement_on_a_module_this_workspace_cannot_import` is what
   keeps this paragraph from re-acquiring an unrunnable measurement, and it is the thing to change
   first if somebody wants the old claim back: put `grpcio` where a `uv sync` installs it, and the
   test permits the sentence.

   And what layer 2 sees is an **import**, which is a different object
   from a channel: three of the four arrive as one (`ctypes`, `_socket`, and a named compiled
   extension such as `grpc`), and layer 2 refuses two of those three. `_socket` and `grpc` are on
   its list; `ctypes` is off it on purpose, for the reason `no_egress.py` gives in the paragraph
   naming its one caller — so "layer 2 cannot see `ctypes`" and "`ctypes` is deliberately off layer
   2's list" are not both available, and only the second is true. The **child process** is the one
   no static reader can help with at all, because `subprocess` is how `pyexec` and `calc` do their
   work. Layer 3 sees none of the four. What is left is `make offline-run`'s, because it takes the
   network away instead of asking Python nicely — and since it is the only cover those two have,
   `make check` runs it wherever the kernel allows an unprivileged network namespace and **names
   it** where it does not, rather than going green with two of the four unverified and nothing on
   screen saying so.
2. **The static scan** (`mcp_server_kit/no_egress.py`), one three-line test per server. AST-based,
   not grep-based — `import httpx as h` and `from requests import get` read differently as text and
   identically as a tree.
3. **The whole suite runs with the guard armed** (root `conftest.py`). A test that only passes by
   reaching the internet fails instead, which is what makes a vendored dataset *proven* sufficient.
   `make offline-run` goes further and takes the network away entirely.
4. **Default-deny egress at the deployment** (`servers/*/deploy/networkpolicy.yaml`), asserted
   fleet-wide by `tests/test_deploy_shape.py::test_the_egress_policy_denies_and_selects_the_workload`
   in **three** directions — `Egress` in `policyTypes`, an empty `egress:`, and a `podSelector` that
   matches the Deployment's own pod label, since a policy bound to no workload denies nothing.
   **That test is new, and this paragraph used to describe the gap without naming it as one**: it
   said the assertion lived in each server's own `test_deploy.py`, which was true and was the whole
   of it — seven copies of one rule, none of them owed by an eighth server. Driven, a server whose
   policy permitted all outbound traffic and which shipped no `servers/*/tests/test_deploy.py`
   passed the entire fleet suite. `tests/test_fleet.py::test_a_server_ships_the_whole_set` now
   requires that file too, for what only it can hold — the server's port, its ingress peers, and
   the Service-to-ServiceMonitor port *name*.

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
a `calculation_key`-style probe if the caller is expected to cache it, and **a ceiling on how many
of it may run at once**. That last one is not the same bound as the first: `servers/calc` bounded
every individual call's atom count and its wall clock and still had no answer for twenty of them
arriving together, on an image that pins one thread per calculation. `engine/admission.py` is the
worked example, and the shape generalises — a full pod refuses promptly rather than queueing,
because a queued minute-long calculation returns after `request_timeout` has expired, computed at
the expense of one somebody is still waiting for. **And the ceiling counts what the pod spends
rather than calls**, because that same pin does not reach a subprocess: CREST is handed a scrubbed
environment and told to use four threads, so a call-counting ceiling of four admitted sixteen
runnable threads on a four-core pod. See `docs/adding-a-server.md`.

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

**"What the pod spends" is not only CPU, and the cheapest server in this fleet needs a ceiling too.**
An MCP *session* is memory — 56.6 kB each, measured on the real `chem` app, growing linearly with
no ceiling and at an arrival rate an idle timeout cannot see — and nothing a tool body can reach.
So that bound is `mcp_server_kit`'s rather than a server's (`MCP_MAX_SESSIONS`, refused with a 503 and
`Retry-After` on the handshake that would mint one), and a new server inherits it with nothing to
write and nothing to set; `D-2026-09-12-a-session-is-memory-nobody-counted` has the arithmetic. Two
things about the CPU half generalise less than they look, and both are in
`D-2026-09-12-one-tool-call-is-not-one-thread`: one tool *call* may be many threads — `rxnpredict`'s
consensus tools fan out over every enabled predictor, measured at six at once — and **whether a tool
is gated is not the manifest's `read_only`/`state_changing` split**, which is how `servers/calc`
derives it and which does not carry: every `servers/chem` tool is `read_only`, correctly, and six
of them — the depiction and the species enumerations, one of which costs 18 s of CPU on a legal
molecule — share a ceiling.

## Ports

**`MODULES.md` is the port registry, and it is the only one.** Claim the next free port there, in
the same pull request that adds the server; `tests/test_fleet.py` checks it against every manifest
in both directions and reads no other file.

There used to be a second table here. It listed fewer servers than were built, and advertised
"8861+ compound identity & data" and "8890+ spectra & analytics" as free over ports 8865 and 8899
that `rxnlabel` and `pyexec` already held — so a session that read this file first would have
claimed a taken port, and the collision would have surfaced only when both pods were scheduled. A
second declaration nothing checks is exactly what `manifests/README.md` forbids for a manifest, and
it does not become safe because the subject is a number.

What belongs here is the *rule* rather than the assignments: the fleet's block is **8850–8899**,
and every manifest in it is what `tests/test_fleet.py` holds — that half is a checked fact.

**The clearance from the rest of the family is not, and the sentence that used to state it was
wrong.** It read "deliberately clear of Chemclaw3's own connectors at 8810–8815", and Chemclaw3's
`bo` connector has sat on **8816** since it was written — outside the range this file published as
that repository's. Nothing here could have caught it: the numbers belong to a repository this one
cannot see, and a test that read them would be reading a checkout that may not exist. That is the
same defect as the port table this section replaced, one repository further out, and it does not
become safe because the subject is somebody else's number.

So the clearance is recorded as the *reason the block starts at 8850*, with the date it was last
checked, rather than as a boundary anybody may rely on — and checking it means **re-reading that
repository**, which is the only thing that can confirm or refute a sentence about a checkout no
test here can open.

Re-read on 2026-09-19 against the repositories in the family. Chemclaw3's connector bundles are
what claim ports there, and one of them — `results` — declares no `endpoint:` at all, so it claims
none. The bundles whose servers that repository runs itself sit below this block: `molfp`, `rxnfp`,
`calc` and `bo`, plus 8810 for the dev process that mounts every bundle by name. Its runbook
additionally names 8000 for the front door and 8820 for a mock OpenAI-compatible LLM, and
`Chemclaw3_mock` serves 8090 and 8091.

**Three of Chemclaw3's endpoints are inside 8850–8899, and that is the seam working rather than a
collision.** They belong to the bundles that declare an endpoint for a server *this* repository
hosts — `chem`, `rxnpredict` and `safety` — so the numbers in them are this fleet's own, and they
are not restated here for the reason no port is: `MODULES.md` is the registry, the manifests are
what it is checked against, and a second copy goes stale. What this paragraph said until today was
"everything observed is below 8850", observed on 2026-08-27; it was already false when it was
written, and read to anybody checking like a range this fleet had walked into. The block still
begins at 8850 and is still fifty ports wide, because nothing that is *not* one of this fleet's own
servers was found inside it.

A collision with one of those would surface in a local full-stack run, not in this repository's
suite. If you find one, move **this** block — renumbering a served port here is a `MODULES.md`
change and a manifest change in the same pull request, and the registry test is what keeps the two
together.

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
| Knowledge graph read/write | core |
| ELN and ORD ingestion | `ingest/sources` |
| Publishing a computed result to an external result store, and re-queueing what a destination refused | `results` |

**The table was re-read against Chemclaw3 on 2026-09-19 and two things in it were wrong.** The
knowledge row used to end "and the PR-gate"; there is no PR-gate —
`D-2026-09-05-the-gate-follows-behaviour-not-knowledge` deleted it and every module behind it, and
`src/chemclaw/kg/git_writer.py` opens by saying so. A capability named in this table as a reason not
to build something here has to still exist, or the table is refusing a server on a ground that has
gone. The `results` row is the opposite correction: that bundle is a capability this table never
listed. It is the one Chemclaw3 bundle with **no `endpoint:`** — jobs only, because a write is not
an agent-facing tool there — so nothing in this fleet dials it and nothing here could have noticed
it existed. It belongs in the table for the ordinary reason: a server here that delivered results to
somebody's database would be a second answer to one question, and it would need an outbound call at
request time, which this fleet does not make.

**There is no DFT row any more, and its absence is a constraint rather than an opening.** Chemclaw3
deleted its `qm` bundle, the Nextflow/HPC launcher behind it and `compute_dft_energy` itself
(`D-2026-08-26-semiempirical-is-the-whole-tier` there): the calculation tier that family runs is
semiempirical end to end — GFN2-xTB via tblite, and CREST — served from `servers/calc/` in a pod on
OpenShift or Databricks. So a server proposed here **must not** reintroduce one by the back door: a
tool that shells out to ORCA, Psi4, Gaussian or NWChem, or that answers from a hosted quantum-chemistry
API, is not a gap this fleet fills. Both halves of the rule bite — the first needs a cluster nobody
has, and the second is an outbound call at request time, which the no-egress posture forbids outright.
Restoring a heavy tier is a decision for Chemclaw3 to take again, in an ADR, before anything is built
here.

Before proposing a tool, check `MODULES.md` and the table above. Overlap that is deliberate must be
argued in the server's README — `rxnsearch` is scoped to *aggregate condition statistics* precisely
because per-record ORD retrieval is already `eln-ord` plus `rxnfp`.

**Rows that have left the table outright: `chem` is now `servers/chem/`, and `safety` is now
`servers/safety/`.** There is a third bundle over there of the same *shape* — `rxnpredict`, whose
manifest calls itself exactly that — and it is **not** a row that left, because it was never a
Chemclaw3 capability: the server was built here and that repository gained a declaration for it,
which is the seam run in the other direction. The distinction is worth keeping because only the
first kind needs the paragraph below; a capability that never moved cannot leave two live
definitions behind it. A capability moving out of Chemclaw3 is the one sanctioned way off this list,
and it is only sanctioned when the move is a *replacement*: same manifest `name`, same tools, same
arguments, so exactly one of the two can be addressed (`CHEMCLAW_CONNECTOR_URLS` is keyed by name,
and `CHEMCLAW_CONNECTORS_DIR` gives the first directory the collision). A port that renamed itself
would leave both live, which is the duplication this section exists to prevent. See
`servers/chem/README.md` and `servers/safety/README.md`.

**`calc` is the third row and it left in a different way, which is why it is struck through only in
part.** Chemclaw3 keeps its `calc` bundle and its whole tool surface; what moved is the *physics
underneath* — exposed here as `servers/calc/`, a **backend** Chemclaw3 calls from inside
`science/calc/store.py::cached_compute` on a cache miss, not a connector it dials. A `calc` manifest
reaching `CHEMCLAW_CONNECTORS_DIR` would let a partial surface win the name collision and remove the
calibration ledger, the calculation cache, the artifact store and every durable job from the agent's
surface, with no error — so **it is not in `manifests/`**. It and `rxnlabel` are registered in
`manifests-internal/`, which no `export` line names, and each declares `mount: backend`, a key
Chemclaw3's `extra="forbid"` manifest model refuses; mounting that directory anyway is a startup
error naming the file. `tests/test_fleet.py` holds both halves. The distinction used to live in
prose, in the five documents that also published the command that breaks it.

`cached_compute` takes the key as an *argument*, so a key that only arrives on the result would be
unusable there — which is why that server serves `calculation_key`, returning the identity of a
calculation before it runs. That is what makes the split honest: Chemclaw3 never derives a key, so
the two `CALCULATION_EPOCH` constants **compose** rather than needing to agree — `remote_key` folds
Chemclaw3's over this server's `params_hash`, so a bump on either side alone invalidates every stored
row. They are moved together by convention (it keeps the two epoch logs readable), not by an
invariant a test can enforce; what `servers/calc/tests/test_key_contract.py` pins is the hash, the
envelope, the flat format and the four field names.

**And it is why that server ships primitives rather than composites.** A calculation whose key names
its own *output* is a loop with state, and a loop with state is a durable job — so
`compute_thermochemistry` is not there, while `relax_structure`, `compute_hessian`, `scan_point` and
the two CREST searches are, each separately keyed. Chemclaw3's activities assemble thermochemistry,
scan profiles, reaction energetics and interaction energies from them, and cache one row per
primitive instead of one per job. See `servers/calc/README.md` and `docs/integration.md`.

## The record, and what is open

This file states the rules. **Why** a rule is the way it is — and the measurement behind it — lives
in [`docs/decisions/`](docs/decisions/), one file per decision, named `D-YYYY-MM-DD-<slug>.md` with
its row in the ledger beside it. There is no numbered sequence here and must never be one: this
repository skips the stage Chemclaw3 had to escape, where allocating a number meant reading
`origin/main` and being stale the moment another session pushed. A merged record is never edited; a
decision that has changed gets a new record. Every record ends with a `## What keeps it true`
section naming the tests that hold it, and `tests/test_decision_log.py` resolves every one of those
names against the suite, so a rename cannot retire a citation in silence.

What is still open is [`docs/BACKLOG.md`](docs/BACKLOG.md), and it is a **queue, not a log**: a
closed row is deleted in the commit that closes it, every row names an anchor a `grep` can open, and
a row about another repository says so because nothing here can check it.
`tests/test_backlog_register.py` opens the anchors and reports the rows it had to skip.

**Neither file may state a count of itself.** `grep -c '^- \[ \]' docs/BACKLOG.md` answers, and
a number in prose is a claim about its author's afternoon — the same argument as the deleted port
table, one document over.

## Working in this repository

```sh
make install         # uv sync
make check           # lint + mypy --strict + the suite with its coverage floor + the offline lane + the audit
make cov             # the suite alone, with coverage measured against `[tool.coverage.report]`
make deps-audit      # the supply-chain step alone: pip-audit over the exported lockfile
make offline-run     # the same suite with the network namespace taken away (`check` runs it where it can)
make run-props       # the reference server on 127.0.0.1:8850
make run-safety      # one per server, on the port that server's own manifest publishes
make run-calc        # the heaviest one — a call here can be minutes or hours, deliberately
```

- Python ≥ 3.11, `uv` workspace, `ruff` (line length 100), `mypy --strict`.
- **Ruff selects `S`, `ASYNC` and `BLE` beyond the obvious set, and `BLE` is the one that pays**
  (`D-2026-09-13-the-rule-that-would-have-caught-it-was-not-the-one-asked-for`). The claim two
  sections up — that every path here which catches an exception and answers anyway classifies it —
  had nothing behind it and one path did not; `BLE001` lands on most of those lines, so each carries
  a `# noqa: BLE001` and its reason *at the site*.
  **It does not land on all of them, and this sentence used to say it did**
  (`D-2026-09-14-a-lint-rule-that-does-not-fire-is-not-the-control-it-was-read-as`): measured,
  `BLE001` is silent on a blind handler that logs with `logging.exception` and returns, and on one
  that re-raises conditionally — two shapes this fleet writes, `rxnlabel`'s mapper and namer among
  them. Worse, the stated remedy is unavailable there: `RUF100` is selected, so a `# noqa: BLE001`
  on a line ruff did not flag is itself an error. So the control is a pair.
  `tests/test_fleet.py::test_every_blind_handler_that_answers_anyway_is_argued` is the half ruff
  cannot reach — a blind handler that answers without re-raising classifies through
  `mcp_server_kit.degradation`, carries the `noqa` ruff did ask for, or is argued in an allowlist
  beside the test, which a second test holds against the tree in both directions. `S101` is deliberately **off**: `tests/test_fleet.py` already holds the no-`assert`
  rule over serving code and carries the argument for its two exemptions, and a `per-file-ignores`
  list would be a second copy of that list with nothing reconciling the two.
- **An image installs what `uv.lock` resolves, not what pip resolves on the day of the build**
  (`D-2026-09-13-an-audit-of-a-lockfile-no-image-reads-audits-nothing`). Every Containerfile copies
  the lock and installs `uv export --frozen`'s output with `--require-hashes`; measured on `props`,
  the re-resolving form shipped 11 of 37 packages the audit had never seen, `mcp` 1.29.0 → 1.30.0
  among them. `docs/BACKLOG.md` names the one install that is still outside it.
- **A published image needs the gate to have run on the revision it is built from.** Jenkins cannot
  see GitHub Actions, so `Preflight` refuses a publishing run with `RUN_GATE` off rather than
  claiming a control in another system
  (`D-2026-09-13-a-gate-in-another-system-is-not-a-gate-this-one-can-see`).
- **No `assert` in serving code.** `python -O` deletes every one of them, so an invariant enforced
  by an assert is a control conditional on how somebody started the process — and an
  `AssertionError` is not a `ValueError`, so what reaches the model is an `error_id` rather than
  something it can act on. Use `if ...: raise`. The only exemption is a module whose *product* is
  an assertion failure (`mcp_server_kit`'s `testing.py` and `no_egress.py`, both imported by tests
  and by nothing else), and
  `tests/test_fleet.py::test_no_serving_module_enforces_an_invariant_with_assert` holds the line.
- The `mcp` SDK is pinned to the **1.x line** deliberately: Chemclaw3 is on `mcp.server.fastmcp`,
  and matching its generation keeps `connector_app` line-for-line comparable with
  `chemclaw.connectors.server`. Moving to 2.x (`MCPServer`) is a deliberate migration for both
  repositories, not a lockfile bump.
- Adding a top-level directory means adding a row to this file and giving the directory a
  `README.md` — GitHub renders one the moment a reader clicks the folder.
- A change to a server's tool surface is a change to its `connector.yaml` in the same commit. The
  test that checks them will fail otherwise, which is the intent.
