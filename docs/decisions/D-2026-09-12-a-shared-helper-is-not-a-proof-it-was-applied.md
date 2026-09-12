# D-2026-09-12-a-shared-helper-is-not-a-proof-it-was-applied — A shared helper is not a proof it was applied

**Status:** accepted · **Date:** 2026-09-12 · **Commit:** wave W22, on top of
`3ca770a`. **No hash is written for this pass**, for the reason
`D-2026-09-12-a-bypass-that-is-not-in-the-suite-is-not-closed` §5 gives: the work merges by squash,
so any hash this session could name is a branch commit `main` will not contain, and
`test_every_commit_the_registers_cite_is_reachable_from_head` refuses one.

## Context

`CLAUDE.md` has stated the rule in the imperative since the fleet grew a server it does not host:

> **Bearer auth enforced on `/mcp` itself.** An external server built on `fastapi-mcp` applies its
> credential as a route dependency, and its MCP surface is *mounted* — a mount bypasses the
> enclosing app's dependencies. Verify against a running server; do not read it off the source.

Every server here goes through one `connector_app`, so the tempting reading is that one proof of
that helper covers all of them. It is the wrong reading, and it is wrong in a specific way: a mount
bypass is exactly the failure where the credential *is* declared, *is* reviewed, and does not reach
the one route that matters. "The helper enforces it" is a statement about the helper; what a
deployment needs is a statement about the composed app.

**What was measured before writing anything, because the brief for this wave asserted the gap was
total and it was not.** Every server's `test_server.py` already drove its real app under uvicorn on
loopback and asserted a bare `POST /mcp` answers 401, and every one of them completed a real MCP
handshake with the right token. Two arms were missing everywhere, and the second is the one with a
recorded incident behind it:

- **A wrong token was checked nowhere against a running server.** So the 401 above was evidence
  that the header was *required*, not that the credential was *compared* — a server that refused
  every request would have passed identically.
- **Fail-closed was checked nowhere against a real app at all.** `packages/mcp_server_kit/tests/test_auth.py`
  covers it, in-process, against a synthetic Starlette app that installs the same middleware — which
  is a proof about `BearerAuthMiddleware` and not about any server. The incident `CLAUDE.md` records
  under that rule is a *deployment* one: Chemclaw3 mounted a secret, recorded the control as
  enabled, and served every tool to anything that could reach the pod, because the serving side
  never checked.

A third arm existed in neither place: the right secret offered under the wrong scheme.

## Decision

`mcp_server_kit.testing.assert_bearer_is_enforced` is the check, and each server's own
`test_server.py` calls it against the uvicorn fixture it already starts. The arms are the anonymous
caller, a wrong token, the right secret under the wrong scheme, the declared credential actually
serving a `tools/list`, and the declared variable *unset* with the right token still offered — then
restored, and re-served, so the refusal is attributable to the unset variable rather than to a
wedged process. `/healthz` is driven through the unset arm too: a probe carries no identity, and a
credential problem must not also take the pod out of the cluster.

**The variable's name is read from the manifest rather than passed in.** `connector.yaml` declares
`token_env`, Chemclaw3 reads that name to send the token, and `app.py` reads it to verify one; the
two agreed in all seven servers when this was written and nothing held them together. Reading the
manifest makes the serving side accountable to the declaration, and the helper additionally asserts
that the token it offers is the value that variable holds — a check that offers a credential the
server was never given proves nothing by being refused.

**A running listener, not an ASGI transport, and not a subprocess.** An in-process
`ASGITransport` would exercise the mount and would be faster; it would also be a claim about a
Python object rather than about a served port, and the rule's own wording is "against a running
server". A subprocess per arm would be closer to production and buys nothing over this, because the
fixture already imports the same module-level `app` a `uvicorn chemclaw_mcp_<name>.app:app` command
loads, and the seven fixtures already exist and are already paid for — so the whole check costs no
server start at all. Measured 2026-09-12 on this pass with `pytest --durations`: **0.43 s to 0.51 s
per server, 3.23 s across the seven.** Whole-file timings were tried first and abandoned as
evidence: on this box the same command ran in 21.4 s and 134.8 s in one session, and in one paired
run the arm *without* these tests was the slower of the two.

**What that lane does not prove, stated because the rule's wording invites the overclaim.** It runs
this repository's `app` object under this repository's uvicorn, in this repository's process. It is
silent about what a *container* starts (a `CMD` naming a different entrypoint, or a `PYTHONOPTIMIZE`
that is not this process's), about anything in front of the pod, and about a server this fleet does
not host — which is precisely the class `CLAUDE.md` wrote the rule for, and which stays a prose
obligation on whoever adds one.

## Consequences

- **An eighth server owes the same proof, and the suite says so.**
  `test_every_server_proves_its_bearer_check_against_a_running_server` reads each server's
  `test_server.py` for the call. It is a shape assertion and could not be anything else — what a
  credential *does* is only visible to a request, which is what the call it looks for makes — but it
  is the level that failed before: `servers/safety/src` once sat outside `make type` for a release
  for exactly this reason.
- **The partial test each server carried is gone rather than kept beside the new one.** Its
  docstring claimed the control and asserted one arm of it, which is the shape this whole wave is
  about.
- **The in-process `test_auth.py` stays.** It is the fast, exhaustive check of the middleware's
  edges — a non-ASCII header, `mode: none`, `/healthz/` with a trailing slash — and none of that
  needs a socket. What it can no longer be read as is a statement about a server.

## What keeps it true

- `servers/props/tests/test_server.py::test_the_bearer_credential_is_enforced_on_the_mounted_mcp_surface`
  and its six siblings, one per server, each against that server's own running listener.
- `tests/test_fleet.py::test_every_server_proves_its_bearer_check_against_a_running_server` — the
  call exists in every server's `test_server.py`, so a new server cannot ship without it.
- `tests/test_fleet.py::test_a_networked_manifest_carries_a_credential` — the declaration half the
  helper reads its variable name from.
- `packages/mcp_server_kit/tests/test_auth.py::test_a_missing_env_var_fails_closed` and
  `test_a_wrong_token_is_refused` — the middleware's own edges, in-process, still checked.
