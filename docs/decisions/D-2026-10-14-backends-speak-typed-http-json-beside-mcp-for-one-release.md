# D-2026-10-14-backends-speak-typed-http-json-beside-mcp-for-one-release — Backends (`calc`, `rxnlabel`) speak typed HTTP/JSON, served beside MCP for one release

**Status:** accepted · **Date:** 2026-10-14

## Context

`calc` and `rxnlabel` are `mount: backend`: no model reads their tools. Chemclaw3 calls `calc`'s
`calculation_key` before every cache lookup and its compute tools on a miss, and `rxnlabel` from a
labelling drain. Today it does that over MCP, opening a session per call (`initialize`, the call,
teardown) because a backend caller has no turn to hold a session across.

MCP buys a backend nothing: the tool list is already fixed by `chemclaw_contracts.calc` and
`.rxnlabel`, which type every request and every answer, and the handshake exists to discover
capabilities nobody is choosing between. It costs time, and the plan's exit for this work is a
`calc` cache-hit round trip of at most 10 ms in cluster.

Measured with `scripts/bench_rpc_vs_mcp.py` at `6100fc0` plus this change, `calculation_key` for
ethanol against the real `calc` app on loopback, p50 in milliseconds, on a 4-CPU box carrying a
load average of about 8 from other jobs (so every row is inflated alike), two runs, server in a
thread of the client's process and as its own process:

| Way of asking | p50 | p95 |
| --- | --- | --- |
| MCP, fresh session per call (what Chemclaw3 does) | 112 to 141 | 195 to 272 |
| MCP, call on a warm session | 16 to 17 | 24 to 39 |
| `POST /rpc/calculation_key`, pooled keep-alive connection | 3.8 to 4.7 | 5.9 to 12.1 |
| the same, a new TCP connection per call | 4.6 to 6.2 | 9.7 to 15.2 |
| `GET /livez` pooled (the HTTP stack, no tool) | 2.1 to 2.3 | 2.9 to 7.6 |

A pooled `/rpc` call is within about 2 ms of the stack's floor, and a warm MCP session still misses
the 10 ms exit by itself, because every result crosses a JSON-RPC envelope and a streamed
response. The in-cluster hop adds the network's round trip to every row alike; Chemclaw3's own
measurement of the cache-hit path (its W6.3) is the exit check.

## Options

1. **Typed HTTP/JSON on the existing app: `POST /rpc/<tool>`.** The body is the tool's
   `chemclaw_contracts` request model, the 200 body is what its response model reads, an error is a
   contract `RpcError`. `connector_app` already owns bearer auth, the `X-Chemclaw-*` headers, the
   body cap, the request metrics, error sanitising and the egress guard, so it adds a route and a
   test helper, no dependency and no build step. The request goes through the same wrapped tool
   manager as `/mcp`, so the two cannot answer differently.
2. **gRPC.** Streaming and a schema language the wire models already are. It needs `grpcio`, a
   `.proto` per backend, generated stubs in two repositories and a second implementation of auth,
   identity, metrics, sanitising and the size cap. `grpc` is on `FORBIDDEN_MODULES` and in ruff's
   banned module list, because it can open arbitrary channels; a backend would need an argued
   exemption in its `test_no_egress.py`, against the fleet's posture. Nothing here needs streaming.
3. **Keep MCP and pool sessions in Chemclaw3** (or run the backends stateless). It removes the
   handshake and keeps one surface, and Chemclaw3 would have to hold sessions per actor and
   connector, expire and re-open them. Even warm it is the 16 ms row above, and the per-call
   session it replaces is the 112 ms row.

## Decision

**Option 1.** It is the only option that meets the exit with no new toolchain, and it reuses every
cross-cutting behaviour instead of reimplementing it. Chemclaw3 selects the wire with a setting
(`calc_transport`, `rpc` or `mcp`; its W6.3), so a bad rollout is a setting, not a revert.

- **The routes.** `connector_app(..., rpc=CALC_REQUESTS)` adds `POST /rpc/<tool>` before the `/`
  mount. Bearer auth, the 1 MB body cap and the request counter are the existing middleware, so an
  unset token refuses every request on `/rpc` as on `/mcp`. Host, Origin and Content-Type are
  upstream's own check, run on the route with the same `MCP_ALLOWED_HOSTS`.
- **Identity** is `X-Chemclaw-Actor/Session/Correlation-Id/Dry-Run`, read from each request by
  `CallerLogMiddleware`: logged, never trusted, the spellings unchanged.
- **Kinds and statuses.** A caller classifies by `kind`; the status is its transport. 200 the
  answer; 400 `refused` (the contract model or the tool refused: the same call cannot succeed);
  503 `at_capacity` (the `[<server>-at-capacity]` marker leads `message`, no transport prefix);
  422 `time_budget` (the pod's clock stopped a call that ran: the input was fine and a retry spends
  the same clock, so it is neither bad input nor a full pod; a 4xx so it is not read as the
  infrastructure's own 504; `[calc-time-budget]` still leads `message`); 500 `failed` (a fault
  replaced by the sanitiser, naming one `error id` that is also in the log); 404 `unknown_op`;
  401, 413 and 421 as on `/mcp`. HEAD, OPTIONS and GET fall through to the mount and answer 404.
- **A refusal is bounded.** `refusal.py` names at most 10 validation errors and cuts any message to
  2,000 characters before the secret scan (`/rpc` formats the first errors itself; on `/mcp` the
  sanitiser cuts what upstream formatted). A body of 70,000 unknown keys was a 12.7 MB
  answer and a 5 s stall of `/livez`, whose liveness probe allows 3 s. A body over 64 KiB is
  validated in a worker thread, so a 200,000-item batch (400,000 errors, seconds of work) leaves
  the loop at a lag of about 0.2 s. On `/mcp` the validation of such a batch, and upstream's formatting of its
  error whole, still run on the loop (`docs/BACKLOG.md`). A body nested past the parser is a 400 on both surfaces.
- **Unknown arguments diverge, in the safe direction.** `/rpc` refuses an argument the contract
  model does not name (`extra="forbid"`); over MCP the tool's own argument model ignores it.
  Callers build requests from the models, so they never send one.
- **Every `refused` is counted.** A body the contract model refuses never reaches the tool manager,
  so the route books it as `chemclaw_mcp_tool_calls_total{outcome="refused"}` for that operation
  (the label set is the served operations).
- **An unterminated body is not bounded here, on either surface.** A request promising 900,000
  bytes and sending one is waited on for as long as the peer holds the connection: uvicorn's
  `--timeout-keep-alive` covers idle connections between requests, the Containerfiles pass no other
  flag, and the Deployments set probes but no request timeout. The caller must hold the bearer, and the NetworkPolicy
  admits Chemclaw3's pods. A read timeout in the body-buffering middleware is `docs/BACKLOG.md`.
- **Metrics.** `chemclaw_mcp_requests_total` gains `path="/rpc/<tool>"` for served operations (any
  other `/rpc/...` is `<other>`); `chemclaw_mcp_tool_calls_total` and the duration histogram count
  the call under the tool, whichever transport brought it. No new label, none about a caller.
- **`contract_version` 1.0.0 to 1.1.0** for `calc` and `rxnlabel`, and `chemclaw-contracts` 1.1.0.
  The manifest's rule makes a new surface a consumer codes against a minor bump, though no tool,
  argument or answer changed. `/healthz` reports 1.1.0, so Chemclaw3 can refuse `calc_transport=rpc`
  against a pod that predates it. No manifest key describes the route: the stand-in manifest model
  mirrors the consumer's `extra="forbid"` one, and `tools:` is both surfaces.
- **MCP stays for one release** so the move is reversible. Its removal, and `manifests-internal/`
  with it, is a later record (the plan's W6.4).
- **Model-facing text is unchanged.** No tool function, signature or docstring is touched. The
  check was a before and after dump of every tool's description and input and output schema (81
  entries, identical); no standing test pins descriptions (`assert_manifest_matches` compares
  argument names and types only), and this record adds none.

## Consequences

Chemclaw3 can drop the per-call MCP session, the cancel-on-timeout patch and the open-session path
for backends. The fleet carries a second surface for one release; the dispatch is shared, so the
tests that hold it are the equality of the two answers and the three arms of the credential.
Admission, the non-finite refusal, the batch cap and the sanitiser live in the tool and the kit,
so a new backend inherits them by passing `rpc=` and a request-model mapping.

**Revisit when:** a backend needs a streamed or server-pushed answer (a progress channel, partial
CREST ensembles), or Chemclaw3 holds a second language's client that cannot read a Pydantic-derived
JSON schema; then gRPC or MCP's own streaming is reopened against that need.

## What keeps it true

- `packages/mcp_server_kit/tests/test_rpc.py::test_a_call_returns_the_tools_structured_answer`
  and `test_the_request_model_is_the_gate_for_the_body` hold the body and the typed gate;
  `test_every_arm_of_the_bearer_rule_applies`, `test_the_body_cap_applies`,
  `test_a_fault_is_replaced_once_and_joined_to_the_log_by_its_error_id`,
  `test_the_metrics_carry_the_operation_and_nothing_about_the_caller` and
  `test_the_egress_guard_is_armed_on_this_path_too` hold what is inherited from `/mcp`;
  `test_a_spent_clock_is_its_own_kind_with_its_marker_at_the_head` and
  `test_a_body_the_model_refuses_is_counted_as_the_tools_refusal` hold the kinds and the count.
- `packages/mcp_server_kit/tests/test_hostile_bodies.py` holds the bounds:
  `test_many_unknown_keys_are_refused_small_and_do_not_stall_the_loop`,
  `test_a_batch_of_empty_items_is_refused_small_and_does_not_stall_the_loop`,
  `test_the_mcp_path_bounds_the_same_messages`,
  `test_a_body_nested_past_the_parser_is_a_refusal_not_a_traceback` and
  `test_the_mcp_surface_answers_a_deeply_nested_body_with_a_400`.
- `tests/test_identity_contract.py::test_the_rpc_surface_reads_every_header_per_request` holds the
  per-request identity under the sender's spellings, across concurrent callers.
- `tests/test_fleet_rpc.py::test_every_declared_tool_is_a_routed_operation_that_validates_its_body`
  and `test_rpc_returns_what_the_mcp_tool_manager_returns` hold, for every `mount: backend`
  server, that each declared tool is an operation and that `/rpc` and MCP answer a fixture alike.
- `tests/test_fleet_auth.py::test_every_backend_proves_its_rpc_surface_against_a_running_server`
  requires each backend's `test_server.py` to drive the credential on `/rpc` and the equality of
  the two wires against a running listener (`servers/calc/tests/test_server.py::test_rpc_and_mcp_answer_the_same_arguments_alike`,
  and rxnlabel's twin).
- The latency table is a measurement, not an assertion: re-run `scripts/bench_rpc_vs_mcp.py`.
