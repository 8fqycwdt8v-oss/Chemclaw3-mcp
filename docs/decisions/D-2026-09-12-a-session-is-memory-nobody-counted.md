# D-2026-09-12-a-session-is-memory-nobody-counted — A session is memory nobody counted

**Status:** accepted · **Date:** 2026-09-12 · **Commit:** wave W23, on top of `91c8f6e`. **No hash
is written for this pass**, for the reason `D-2026-09-12-a-bypass-that-is-not-in-the-suite-is-not-closed`
§5 gives: the work merges by squash, so any hash this session could name is a branch commit `main`
will not contain, and `test_every_commit_the_registers_cite_is_reachable_from_head` refuses one.

## Context

`mcp_server_kit/sessions.py` already fixes the *leak*: upstream never passes
`session_idle_timeout`, and a `DELETE` takes the one branch that skips upstream's own cleanup, so a
session used to stay for the life of the process. That module adds an idle reaper and a sweep, and
holds the deadline off while a tool call is running so a four-hour CREST search is not cut as
"idle".

**Nothing bounded how many sessions exist.** `StreamableHTTPSessionManager._server_instances` is a
plain dict that upstream adds to on every `initialize` with no admission of any kind, and there was
no `MCP_MAX_SESSIONS`. A timeout bounds how long one session lives; it says nothing about the rate
at which they arrive, and the two are different controls.

### What was measured, because the brief's own figures did not reproduce

Driven against the real `chem` app under uvicorn, with the server in its own process so the RSS is
the pod's and not a client's:

| Sessions opened, never deleted | RSS growth | Per session |
| --- | --- | --- |
| 200 | 11,056 kB | 56,607 B |
| 400 | 22,264 kB | 56,996 B |
| 600 | 33,304 kB | 56,839 B |
| 800 | 44,284 kB | 56,684 B |
| 1,000 | 55,292 kB | 56,619 B |

Strictly linear, flat to three digits at every mark. A session that has been *used* — GET stream,
`tools/list`, one `tools/call`, then abandoned — starts higher and converges on the same marginal
figure: 126 kB at 50 sessions falling to 87 kB at 300, whose last three 50-session increments are
56, 64 and 60 kB.

**So the 149 kB in `sessions.py`'s own header does not reproduce, and it is corrected there rather
than quietly dropped.** The direction matters in both halves: the leak is two and a half times
*cheaper* per session than that file claimed, which makes the reaper less urgent than it read — and
the ceiling more so, because the number of sessions one caller can hold inside a 512Mi pod is two
and a half times larger than a reader of that paragraph would have assumed.

The rate is the other half. One client on loopback opened **261 handshakes per second**. So the
brief's framing — "~1 session/s arrival, 1,800 sessions before the first expiry, 268 MB" — describes
the *honest* caller: at the measured cost that steady state is ~102 MB, uncomfortable inside a 512Mi
limit and not fatal. What the idle timeout cannot see at all is a burst: 30 minutes at the rate one
authenticated caller can actually dial is hundreds of thousands of sessions, and nothing has been
idle long enough to reap any of them. An OOMKill then takes every session on the pod down together —
including the CREST search the hold-open logic exists to protect.

## Decision

**`MCP_MAX_SESSIONS`, enforced on the request that would mint a session, in `mcp_server_kit`.**

- **In the kit, not beside `servers/calc/engine/admission.py`.** A session is the *transport's*
  object: every server in this fleet has the same one, pays the same 56.6 kB for it, and no tool
  body can see it. An admission ceiling on calls and a ceiling on sessions are not the same bound
  and neither implies the other — `props` gates no call at all and still holds sessions.
- **At the session manager's own ASGI entry**, which is the last seam before upstream decides
  whether to mint a transport and the only one where a refusal costs nothing: no session, no anyio
  task and no memory object streams exist yet. That is what makes it admission control rather than
  a clock — nothing is abandoned mid-flight, because nothing was started.
- **Refused promptly, never queued**, the same argument `servers/calc/engine/admission.py` makes one
  layer down. Measured on one shared connection, where the probe's own threads are out of the way: a
  refusal is **0.95 ms** against **3.16 ms** for an admitted handshake. A refusal does no work, so it
  is cheaper than the admission it replaced.
- **The refusal is an HTTP status, not a marker token**, and that is the difference from the tool
  path. `servers/calc` needs `AT_CAPACITY_MARKER` because a refused *tool call* has no channel but
  its own text — the protocol flattens every exception into one untyped `isError=True` block. A
  refused handshake is a plain HTTP response on `/mcp`, where 404 already means "unknown session
  id" and 200 means served, so **503 with `Retry-After`** is unambiguous and the body needs to carry
  no convention at all. The JSON-RPC code is a server-defined `-32000` rather than upstream's
  `INVALID_REQUEST`, so a full pod cannot be read as a stale session id.
- **Three metrics, none of which can name a caller.** `chemclaw_mcp_sessions_live`,
  `chemclaw_mcp_sessions_ceiling` and `chemclaw_mcp_sessions_refused_total`, each labelled by server
  and nothing else. A session *count* is not a session *id*; `/metrics` is unauthenticated and the
  rule it keeps is untouched.

**The default is 1,024, and it is arithmetic over two measured numbers.** The smallest memory limit
in this fleet is 512Mi (`props`, `chem`, `safety`); a backlog of sessions may hold an eighth of it,
because a session is overhead left over from turns nobody finished and the pod's memory is for the
corpus it serves and the calls in flight; 64 MiB divided by 56.6 kB is 1,158, rounded down to a
round 1,024 so the budget is a bound rather than a target.

**That default is deliberately below the reaper's own worst-case steady state**, and this is the
trade the bound exists to make. At Chemclaw3's ~1 session/s with a 1,800 s timeout, a *fully*
abandoned pod settles at ~1,800 sessions and would be refusing new handshakes. That is the right
answer: losing one turn costs one turn, and the OOMKill it prevents costs every session on the pod.
Normal operation is nowhere near it — the drain's `DELETE` is reclaimed by the sweep that already
exists, so live sessions track concurrent turns rather than turns ever taken.

## Consequences

- **A pod at its ceiling still serves every session it has.** The bound is on *new* sessions only.
  Without that the ceiling would convert a memory bound into an outage, which is strictly worse than
  the leak, so it is a test rather than an intention.
- **The sweep became load-bearing.** `_drop_terminated_sessions` runs before the count is taken:
  upstream's cleanup skips a terminated transport, so a pod whose callers all said goodbye would
  otherwise refuse at a ceiling of corpses. That function existed already; the ceiling is what makes
  its absence fatal rather than merely wasteful.
- **The kit cannot see the number its default rests on**, so the fleet suite checks it. A server
  resized to 256Mi would halve the budget with every assertion inside `packages/mcp_server_kit`
  still green — the same coupling `servers/pyexec` shipped as two transcribed copies of a
  Deployment's CPU limit.
- **`MCP_MAX_SESSIONS` joins the derived inventory** `tests/test_fleet.py::numeric_env_bounds`
  builds, so the ratchet refuses any shipped Deployment or Containerfile that moves it without an
  argued row. No shipped file sets it, which is the point: the default has to be right for the
  smallest pod in the fleet unaided.
- **`0` turns the ceiling off**, and no ceiling gauge is published when it is — a
  `chemclaw_mcp_sessions_ceiling` of 0 would read as "this pod admits nothing", the opposite of what
  the knob means.

## What keeps it true

- `packages/mcp_server_kit/tests/test_session_ceiling.py::test_a_full_pod_refuses_the_next_handshakes_promptly_and_counts_every_one`
  — twelve handshakes arriving together at a saturated pod, all refused, counted, and no slower than
  the same twelve *admitted* by an unbounded pod in the same test. The comparison is the assertion:
  a wall clock here measures the probe's threads, not the server.
- `packages/mcp_server_kit/tests/test_session_ceiling.py::test_a_session_costs_about_what_the_ceiling_was_derived_from`
  — 400 un-deleted handshakes against a real server, RSS read from `/proc`. The default is the
  budget divided by this constant, so a session that started costing ten times as much would put the
  shipped ceiling 10x over its budget with every other assertion here still green.
- `packages/mcp_server_kit/tests/test_session_ceiling.py::test_a_full_pod_still_serves_the_sessions_it_already_has`
  — the bound is on new sessions, not on the pod.
- `packages/mcp_server_kit/tests/test_session_ceiling.py::test_a_session_that_says_goodbye_frees_its_slot`
  — the sweep, through the ceiling.
- `packages/mcp_server_kit/tests/test_session_ceiling.py::test_with_the_ceiling_off_the_pod_admits_past_it`
  — the counterfactual, without which a ceiling that did nothing would look like this suite.
- `packages/mcp_server_kit/tests/test_session_ceiling.py::test_the_default_is_derived_from_the_smallest_pod_and_the_measured_session_cost`
  and `test_the_ceiling_is_an_environment_variable_and_zero_turns_it_off`.
- `tests/test_fleet.py::test_the_session_ceiling_is_derived_from_the_smallest_pod_this_fleet_actually_ships`
  — the input the kit cannot see, read from every shipped Deployment.
- `tests/test_fleet.py::test_no_shipped_deployment_moves_a_bound_the_code_reads_from_the_environment`
  — the ratchet the new variable lands in.
- `packages/mcp_server_kit/tests/test_upstream_surface.py::test_a_session_is_minted_exactly_when_the_session_id_header_is_absent`
  — the one new coupling to a shape upstream never promised. The ceiling decides *before* upstream
  does whether a request will cost memory, and it decides on the header alone; a release that minted
  on `method == "initialize"` instead would leave the gate covering a set of requests that no longer
  overlaps the expensive ones, with every behavioural test above still green.
