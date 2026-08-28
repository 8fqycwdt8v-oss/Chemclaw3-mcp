# Adding a server

Read [`CLAUDE.md`](../CLAUDE.md) first — it is the *why*. This is the checklist.

The reference server is `servers/props/`. Copy its structure rather than inventing one; the
variance between servers should be in what they compute, not in how they are shaped.

## Before you write anything

1. **Check it is not already a Chemclaw3 capability.** The exclusion table is in `CLAUDE.md`. A
   second implementation of a capability is a second answer to one question.
2. **Check it can work with no egress.** If the value depends on a live third-party API, the module
   is rejected or redesigned around a snapshot. There is no third option.
3. **Claim a name and a port in `MODULES.md`,** in this same pull request. The name is used four
   times and they must match: directory, package suffix, manifest `name:`, and the key Chemclaw3
   addresses it by.
4. **Decide whether the tool is request/response or orchestration.** This used to read "decide
   whether any tool can exceed ~20 s", and that was the wrong question — duration is not the
   property this fleet promises. `servers/calc` runs CREST searches that take hours.

   The property is **statelessness**: a tool takes its arguments, computes, and returns. It holds no
   job record, offers no resumption, and if it is interrupted the caller simply calls again. A
   *composite* — optimise, take a Hessian, displace along the imaginary mode, repeat — is a loop
   with state, and the giveaway is that **its key names its own output**, so a caller cannot ask
   "have I computed this?" before running it. That belongs in Chemclaw3 as a durable job; its
   *parts* belong here, each separately keyed.

   `compute_thermochemistry` is the worked example and it went both ways before it settled: it was
   ported, found to be underivable, nearly deleted outright, and finally **decomposed** —
   `relax_structure` + `compute_hessian` here, the RRHO partition functions and the refinement loop
   in Chemclaw3. The measurement that decided it: repeating thermochemistry in Chemclaw3 costs
   0.007 s against 0.816 s cold for ethanol and 0.012 s against 3.273 s for ethyl acetate, two
   orders of magnitude that come entirely from the *nested* caches. Shipping the composite would
   have converted every repeat into a full recompute; decomposed, every one of those hits still
   hits.

   So a slow tool is fine and a stateful one is not. What a slow tool owes the fleet:

   - a **bound on its input** so the cost cannot run away unpriced;
   - a `request_timeout` in its manifest that states the real budget rather than inheriting a
     habitual one and dying mid-calculation;
   - a docstring that tells the model what it is asking for;
   - a `calculation_key`-style probe, if a caller is expected to cache it;
   - and a **ceiling on how many of it may run at once**, refused promptly rather than queued. This
     is not the first bullet again: `servers/calc` bounded every individual call's atom count and
     its wall clock and still had no answer for twenty of them arriving together on an image that
     pins one thread per calculation. Queueing is the wrong answer for a tool this slow — the held
     call comes back after `request_timeout` has expired — so a full server refuses, in a message
     naming the ceiling and the setting that raises it. See
     `servers/calc/src/chemclaw_mcp_calc/engine/admission.py`.

## The files

```
servers/<name>/
├── connector.yaml               # every tool classified read_only or state_changing, exactly once
├── pyproject.toml               # this server's dependency closure and nobody else's
├── Containerfile                # rootless, dataset baked in, no credential for anything external
├── README.md                    # what it serves, the exact artifact it reads, and who refreshes it
├── deploy/networkpolicy.yaml    # policyTypes includes Egress; egress: []
├── deploy/service.yaml          # one port named `http`; what the ServiceMonitor resolves through
├── deploy/servicemonitor.yaml   # path /metrics, port: http — a *name*, not a number
├── src/chemclaw_mcp_<name>/
│   ├── engine/                  # pure computation — no FastAPI, no MCP, no network
│   ├── tools.py                 # the FastMCP surface
│   ├── app.py                   # connector_app(server, name=..., token_env=...)
│   └── data/                    # records + dataset.json
└── tests/
    ├── test_dataset.py          # the corpus validated against itself
    ├── test_tools.py            # what the tools answer, and what they refuse
    ├── test_no_egress.py        # three lines; see props
    ├── test_deploy.py           # the NetworkPolicy, asserted in both directions
    └── test_server.py           # real socket, real handshake, real 401, manifest mirror
```

Then symlink the manifest — never copy it — into the bucket that says what the server *is*:

```sh
mkdir -p manifests/<name>                                          # a connector Chemclaw3 dials
ln -s ../../servers/<name>/connector.yaml manifests/<name>/connector.yaml
```

`manifests/` is what every published `export CHEMCLAW_CONNECTORS_DIR=...` line names, and Chemclaw3
enables everything it discovers there. If the new server is **not** something the agent should see
as tools — a backend called from inside Chemclaw3's own code, or primitives for a background drain
— it goes in `manifests-internal/` instead, and its `connector.yaml` declares `mount: backend`:

```sh
mkdir -p manifests-internal/<name>                                 # reached by configuration only
ln -s ../../servers/<name>/connector.yaml manifests-internal/<name>/connector.yaml
```

That key is refused by Chemclaw3's `extra="forbid"` manifest model, which is the point: a deployment
that mounts the directory anyway gets a startup error naming the file rather than an agent whose
tool surface quietly changed. A connector's manifest must carry no `mount:` key at all, for the same
reason. `tests/test_fleet.py` checks both directions.

## The dataset

**A server with no dataset is possible and `calc` is the first one** — every number it returns is
computed from its dependencies' own compiled parameters (tblite's GFN Hamiltonians, RDKit's Crippen
and QED tables) rather than read from a corpus, so there is no `data/` and no `test_dataset.py`. The
obligation does not disappear with the file; it moves. What replaces "validate the corpus against
itself" is **proving the computation needs nothing from outside the process**, which
`servers/calc/tests/test_no_egress.py` does by running one of each kind of calculation with the
egress guard armed. The failure being ruled out is the one a numerical library can produce: fetching
parameters, model weights or a licence check on first use.

`data/dataset.json` needs all six fields — `name`, `version`, `licence`, `retrieved_from`,
`description`, `sha256` — and `load_dataset` refuses without them. Compute the checksum with
`sha256sum` and paste it; the loader verifies it on every start.

**Then make the corpus validate itself.** This is the part most worth the effort, and `props`
shows the pattern: CAS check digits, molecular weight against formula, and Antoine constants against
the tabulated boiling point are all pairs of *independently written* numbers that must agree, so a
transposed digit fails a test instead of answering a question. Find the equivalent for your data
before falling back to "it was reviewed once".

## Observability

**This section exists because its absence was the cause.** The checklist you are reading never
mentioned logging, metrics or monitoring, and the result was measurable: seven servers, ten metric
series (all ten `prometheus_client` built-ins), 26 log call sites in the whole repository and not
one of them recording a tool name, a duration or an outcome. Four servers contained no log
statement at all. Nobody skipped a step; there was no step.

Most of it you get for free and must not re-implement:

- **Logging is configured by the app `connector_app` returns** (`mcp_server_kit.logging`), from its
  `lifespan` and with `force=True` — because `FastMCP.__init__` has already called `basicConfig` by
  then, and because doing it in `connector_app` itself made reconfiguring the *importing* process's
  root logger a side effect of `import <your server>.app`. Never call `basicConfig`, `dictConfig`
  or `setLevel` in a server: `MCP_LOG_LEVEL`, `MCP_LOG_FORMAT` and `MCP_LOG_JSON` are the knobs,
  and a module just does `logging.getLogger(__name__)`.
- **Every record carries the caller** — actor, session and correlation id, stamped onto the record
  by `ContextFilter` — and every credential this process holds is already scrubbed from the
  message, the traceback, the `extra=` fields and logging's own error diagnostic. Do not format an
  identifier into a message by hand.

  **What a *rendered line* shows is a format question, and the two are not the same claim.** The
  default text format is `[%(correlation)s/%(session)s]`, so the actor is on the record and not in
  the line; `MCP_LOG_JSON=true` publishes all three as `correlation_id`, `session_id` and `actor`,
  which is the shape Chemclaw3's log stack parses and what a deployment should set.
- **Every tool call is already counted and timed**, by server, tool and `ok`/`refused`/`failed`.
  Do not add a per-tool counter in a server; it would be a second answer to one question.

What a *new server* still owes:

1. **A `readiness` callable**, if the server has anything to be unready about — a corpus, a binary,
   a compiled rule table. It loads what the first tool call would load and returns the `Dataset`s
   it verified; `connector_app` answers 503 with the reason when it raises. Without one `/healthz`
   is a constant 200, which is how a pod with a corpus that failed its checksum passes the kubelet
   probe, takes traffic and fails every call.
2. **A metric for anything expensive the server can do to itself** — a killed subprocess, a spent
   budget, a resource limit firing. `servers/calc/src/chemclaw_mcp_calc/engine/metrics.py` and its
   `pyexec` sibling are the two worked examples. The rule for a label is the one
   in `packages/mcp_server_kit/metrics.py`: `/metrics` is unauthenticated, so **never** an actor, a
   session, a correlation id or a tool argument, and never a value a caller chooses unless it is
   clamped to a set this repository defines.
3. **A log line on every branch that ends a run early.** The test is whether an operator could tell
   your three failure modes apart from the log alone. A timeout that only raises is invisible: the
   exception is a `RuntimeError`, so it arrives at `connector_app` as "a tool raised an unexpected
   exception", which is what a genuine bug looks like.
4. **`deploy/service.yaml` and `deploy/servicemonitor.yaml`**, copied from any server and renamed.
   The NetworkPolicy already admits the monitoring namespace; these are what tell Prometheus to use
   it. `tests/test_fleet.py` requires both files and `tests/test_deploy.py` holds their port against
   the Containerfile and the manifest.

## The tool docstrings

They are the prompt. For each tool:

- What is it for, in the words a chemist would use to ask for it.
- Every unit, on every argument and every returned field.
- What the answer is **not** evidence of.
- Where the number came from (`source`), and by which method when there is more than one.
- What it does when it does not know: refuse and name the corpus, never approximate.

## Before you open the pull request

```sh
make check          # lint + mypy --strict + the whole suite
make offline-run    # the same suite with the network taken away
```

Then wire it to a real Chemclaw3 checkout following [`integration.md`](integration.md) and ask the
agent a question only this server can answer. Confirm the tool was called — `source` in the answer
is the tell — and check `/readyz`, because an unreachable connector degrades silently rather than
erroring.

Finally, update `MODULES.md`'s status row and the port table in `CLAUDE.md` if a block changed.
