# D-2026-09-12-a-ceiling-read-before-the-mint-is-a-ceiling-a-burst-walks-past — A ceiling read before the mint is a ceiling a burst walks past

**Status:** accepted · **Date:** 2026-09-12 · **Commit:** wave W23 follow-up, on top of `085f636`.
**No hash is written for this pass**, for the reason
`D-2026-09-12-a-bypass-that-is-not-in-the-suite-is-not-closed` §5 gives: the work merges by squash,
so any hash this session could name is a branch commit `main` will not contain.

Supersedes four sentences of `D-2026-09-12-a-session-is-memory-nobody-counted`, which is otherwise
right and is not edited.

## Context

That record bounded how many MCP sessions a pod may hold, and stated the adversary in its own
words: *"What the idle timeout cannot see at all is a **burst**"*. The control it shipped does not
hold against that adversary, and three further properties of a full pod were argued nowhere.

## 1. The ceiling was a check-then-act race, and a burst walked straight past it

`apply_session_ceiling` read `len(manager._server_instances)` on the session manager's ASGI entry
and refused if that count had reached the ceiling. Upstream registers a session inside
`_handle_stateful_request`, behind `async with self._session_creation_lock` — several awaits after
that read. Nothing increments anything in between, so every request of a concurrent burst observes
the **pre-burst** count and every one of them is admitted.

Driven against the real `connector_app` under uvicorn, all handshakes released from one
`threading.Barrier`:

| configured ceiling | released together | admitted | live sessions |
|---|---|---|---|
| 8 | 64 | 64 | **64** (8x) |
| 16 | 128 | 99 | **99** (6x) |
| 64 | 256 | 256 | **256** (4x) |

The overshoot is bounded only by the caller's connection width; uvicorn sets no
`limit_concurrency`. A serial caller was bounded correctly, which is why every shipped test passed:
`test_a_full_pod_refuses_the_next_handshakes_promptly_and_counts_every_one` opens its sessions **one
at a time** in the fixture and only bursts against an already-full pod, so the window between the
check and the mint is never open when the burst arrives.

**The fix takes the slot before the await.** An admitted handshake increments a counter this
function owns, under a `threading.Lock` with no `await` between the read and the increment — the
shape `servers/calc/engine/admission.py` already uses one layer down — and the reservation is
handed over to `_server_instances` on the response's first ASGI message, because upstream cannot
write a byte before it has registered the transport. Watching `send` rather than the request's
return matters for one case: a sessionless `GET`, which upstream also mints for, holds its
connection open, and would otherwise hold a second slot for the life of the stream.

**The count stays derived from the map, deliberately.** The reservation covers only the window
between the check and the registration, and is released in a `finally` as well as on the response,
so a cancellation or a client hang-up cannot strand one. Everything after that window is
`len(_server_instances)`, which a reap, a `DELETE` or a crash empties by itself. A counter that
*replaced* the map would need decrementing on every one of those paths, and the one it missed would
wedge the pod permanently — that structural property of the original design is the half worth
keeping, and it is kept.

## 2. Every request the server itself answered 400 minted a session nobody could use

Upstream mints on the **absence** of the session-id header and on nothing else — not the method,
not the body. Measured on the real app, `live 0 -> 1` for each of: a bare `DELETE /mcp`, a bare
`GET /mcp`, a `ping` with no session id, and a malformed body. Every one answers **400 Bad
Request**. The cheapest of them runs at **476 requests/s** from one client, which fills the shipped
1,024-session ceiling in about two seconds using the simplest request anybody can construct — and
the caller was told its request was bad, so it never learns it owns anything.

They are nameable, which is what makes the fix exact rather than a heuristic: upstream puts
`mcp-session-id` on the 400 as well as on the 200, so the response names the session it just
orphaned. `_discard_an_unusable_session` terminates and pops it, in that order and both — the pop
alone leaves the session's anyio task blocked in `Server.run` forever, and `terminate()` alone is
the case `_drop_terminated_sessions` already exists for.

## 3. A handshake is not a conversation, and the two did not deserve the same lease

The 1,800 s idle lease is the right bound on a session a chemist is between tool calls on. It is
three orders of magnitude too generous for one that was minted and never spoken to again, and that
gap is what turned the ceiling into a denial of service rather than a memory bound: one
authenticated caller opens handshakes at 261/s, fills the ceiling in about four seconds, and no
slot is refunded for thirty minutes, during which every other caller is refused.

`MCP_SESSION_UNUSED_TIMEOUT_SECONDS` (60 s, clamped to the idle timeout) is the lease a session
starts on. Nothing has to promote it: upstream already pushes the deadline to the full timeout on
**every** request for an existing session, and an MCP client sends `notifications/initialized`
microseconds after the handshake returns. What is left holding the short lease is exactly the
session nobody came back for.

It is never applied over a session that is already working. A hold-open deadline of `math.inf` — a
tool call in flight — and a session that has had a request of its own are both refused, because
overwriting the first would cancel that call from inside the transport with no JSON-RPC error
written anywhere, which is the precise failure `_hold_open_during_tool_calls` exists to prevent.

## 4. `Retry-After: 1` was an interval the pod cannot honour

A refusal costs this pod **0.792 ms** (median of 50, one shared connection, measured against the
real app). At `Retry-After: 1`, `DEFAULT_MAX_SESSIONS` displaced callers obeying it spend
`1024 x 0.0008 = 0.8` of a core on being told no — on a pod limited to two, the refusal path becomes
the load, and every refusal also wrote a `logger.warning`. It is **10 s**, which holds that under a
tenth of a core and is short enough that a caller whose turn is still worth having has not given up.
It is deliberately *not* raised to the reclaim horizon: a pod that is merely busy frees slots as
callers finish, in seconds, and a minute-long `Retry-After` would turn a two-second saturation into
a minute-long outage for everything that obeyed it. The warning is rate-limited to one line per
10 s; `chemclaw_mcp_sessions_refused_total` stays exact and is what the line points at.

## 5. Readiness is deliberately unchanged, and that is the hardest call here

`CLAUDE.md` says "`/healthz` is readiness, not a constant 200", and a pod at its ceiling answers
200. The obvious reading is that the rule is being broken; it is not, and flipping it would be the
larger harm.

An MCP session is **sticky to the pod holding it**. A pod that reports unready leaves the Service
endpoints, so the sessions it is still serving — up to 1,024 of them, including a CREST search four
hours in — become unreachable. That converts "cannot admit a new session" into "loses everything in
flight", which is strictly worse than the condition it reports. `servers/chem/deploy/deployment.yaml`
points **both** probes at `/healthz`, so the same flip would additionally restart the pod under any
fleet-wide load spike: a partial outage becomes a restart storm.

The rule the endpoint does hold is the one it was written for: `/healthz` answers 503 when the
server *cannot serve what it advertises* — a corpus that failed its checksum, a backend that will
not load. Being full is not that. What makes a full pod visible is
`chemclaw_mcp_sessions_live` against `chemclaw_mcp_sessions_ceiling`, both already published and
both labelled by server, which is a saturation alert rather than a probe. **The residual is
recorded rather than closed**: a caller with a valid credential can still hold a pod at its ceiling
for 60 s at a time, and no session ceiling can prevent that — what it prevents is the OOMKill that
takes every other session down with it.

## 6. `_session_owners` — the map the sweep does not touch

`_drop_terminated_sessions` sweeps `_server_instances` and not upstream's parallel
`_session_owners`, which upstream pops alongside on every one of its own removal paths, including
the `finally` a polite `DELETE` skips. That is sound today for exactly one reason: this fleet's
`BearerAuthMiddleware` never puts an `AuthenticatedUser` in `scope["user"]`, so `requestor` is
`None` and upstream records nothing. Measured by making the kit set it: 2 of 2 politely deleted
sessions then leaked into a map nothing sweeps. Both halves are now asserted rather than believed.

## 7. A correction to `D-2026-09-12-a-session-is-memory-nobody-counted`

That record says: *"64 MiB divided by 56.6 kB is 1,158, rounded down to a round 1,024"*. Neither
half reproduces. 67,108,864 ÷ 56,600 is **1,185.7**, and ÷ 57,900 — `SESSION_COST_BYTES`, the
constant actually used — is **1,159.0**. And 1,024 is not the quotient rounded down; it is a
power-of-two round-down that discards 13% of the budget. The bound itself is safe and internally
consistent (1,024 × 57,900 = 59.3 MB against a 64 MiB budget), and
`test_the_default_is_derived_from_the_smallest_pod_and_the_measured_session_cost` re-derives it
from both constants, which is why the sentence was wrong and the code was not. The arithmetic is
the code's; no figure is transcribed here to replace it.

## What keeps it true

- `packages/mcp_server_kit/tests/test_session_ceiling.py::test_a_simultaneous_burst_against_an_empty_pod_cannot_admit_past_the_ceiling`
  — §1, driven: handshakes released from one barrier against an **empty** pod, asserted on the
  live instance map as well as on the statuses. It fails against the shipped implementation.
- `packages/mcp_server_kit/tests/test_sessions.py::test_a_request_the_server_refused_leaves_no_session_behind`
  — §2, the four requests that each minted a session, asserted on the map rather than on the status
  codes, which were always right.
- `packages/mcp_server_kit/tests/test_sessions.py::test_a_handshake_nobody_came_back_for_is_reaped_long_before_a_session_in_use`
  — §3 as a differential: both sessions opened the same way, one of them spoken to once.
- `packages/mcp_server_kit/tests/test_sessions.py::test_the_short_lease_is_never_applied_over_a_session_that_is_already_working`
  — §3's two guards, driven against the decision rather than against a schedule.
- `packages/mcp_server_kit/tests/test_sessions.py::test_the_first_lease_is_its_own_knob_and_is_never_longer_than_the_idle_timeout`
  — the knob and the clamp.
- `packages/mcp_server_kit/tests/test_session_ceiling.py::test_the_refusals_status_and_retry_interval_are_what_a_client_is_told`
  — §4, both numbers pinned as literals, because every other assertion in that file compared
  `AT_CAPACITY_STATUS` to itself.
- `packages/mcp_server_kit/tests/test_session_ceiling.py::test_a_pod_that_is_refusing_says_so_once_an_interval_and_not_once_a_refusal`
  — §4's log half.
- `packages/mcp_server_kit/tests/test_upstream_surface.py::test_a_session_is_recorded_in_a_second_map_only_for_an_authenticated_scope_user`
  and `packages/mcp_server_kit/tests/test_sessions.py::test_a_politely_deleted_session_is_reclaimed`
  — §6, the condition and the behaviour.
- `packages/mcp_server_kit/tests/test_session_ceiling.py::test_the_default_is_derived_from_the_smallest_pod_and_the_measured_session_cost`
  — §7: the arithmetic that is real, unchanged and already in the suite.
