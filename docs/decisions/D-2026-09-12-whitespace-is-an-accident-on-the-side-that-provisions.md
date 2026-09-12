# D-2026-09-12-whitespace-is-an-accident-on-the-side-that-provisions — Whitespace is an accident on the side that provisions

**Status:** accepted · **Date:** 2026-09-12 · **Commit:** wave W23, on top of `91c8f6e`. No hash is
written for this pass, for the reason
`D-2026-09-12-a-bypass-that-is-not-in-the-suite-is-not-closed` §5 gives.

## Context

`BearerAuthMiddleware` compared `offered.strip()` against an unstripped `expected`. Driven with raw
sockets against a running `props` — 400 meaning "past the gate", 401 meaning refused — every one of
`Bearer <tok> `, `Bearer  <tok>`, `Bearer \t<tok>` and `Bearer <tok>\t  ` authenticated, while
`Bearer <tok>extra` and a truncated token were refused.

That is not a bypass: the secret is still required, and a one-byte-different token is still refused.
It is an **asymmetry**, and the asymmetry points the wrong way. The side where whitespace is a
caller being sloppy was lenient; the side where whitespace is an *accident of provisioning* — a
Kubernetes Secret written with `echo`, a `.env` line, a here-doc — was strict. A secret carrying a
trailing newline therefore refused every request, including one offering exactly those bytes, and
the 401 named nothing that would let an operator find it.

It was asserted in neither direction. The same review drove a large attack surface against this
control and could not break it — one-byte-different tokens, prefixes and suffixes, empty tokens,
`Basic`/`Token`/`Negotiate` carrying the right secret, the token as a query parameter, cookie,
`x-api-key` and `proxy-authorization`, duplicated `Authorization` headers in both orders, thirteen
path variants including `/mcp/../mcp` and `/mcp%2f`, five anonymous methods, the variable set to
empty and to whitespace, and session reuse across a rotation. This is the one crack in it.

## Decision

**Normalise both sides, and write down what that costs.**

`expected` is stripped where it is read, so the `not expected` arm and the comparison see the same
value; `offered` stays stripped as before. The two consequences are stated rather than discovered:

- A secret provisioned with surrounding whitespace works, which is the accident this fixes.
- **A secret and the same secret with surrounding whitespace are the same secret**, so rotating
  between them rotates nothing. That was already true of the caller's half and nothing said so.

**Stripping neither side was the alternative and is rejected.** It is the strictest reading and it
fails closed, but the failure is an outage: a newline-provisioned secret takes the whole server off
the network with a 401 that names nothing, and no operator can express "a token with a trailing
space" as an intent worth honouring. Between an unstated tolerance and an unstated trap, the
tolerance is the one to keep and state.

**`expected` is stripped at the read rather than at the comparison, and in exactly one place.**
The read is the right site because the `not expected` arm must see the same value the comparison
does: a variable holding only whitespace has to be an *unset* credential and fail closed, not an
empty secret an empty offer matches. The "exactly one" half is not style — the first version of
this change stripped at both sites, and the mutation that removes the read-site strip then passed
every test, because the comparison stripped it again. A normalisation with two homes is one a
mutation of either cannot reach.

## Consequences

- **`assert_bearer_is_enforced` gains three arms**, so the tolerance is asserted in both directions
  and the fail-closed reading of whitespace-only is asserted beside it. Every server inherits them,
  since each one's `test_server.py` calls that helper against its own running listener.
- **Two strips became one.** See above: the redundancy was invisible in review and visible to a
  mutation, which is the argument for running one.
- **The trailing-tab variant the raw-socket probe found is not in the suite**, and the reason is in
  the helper: h11 refuses to send a field value with leading or trailing whitespace, so it is not
  expressible through an HTTP client at all. The arm that is there pads *inside* the value
  (`Bearer  <tok>`) and reaches the same `strip()`.

## What keeps it true

- `packages/mcp_server_kit/src/mcp_server_kit/testing.py::assert_bearer_is_enforced`, driven by
  `servers/props/tests/test_server.py::test_the_bearer_credential_is_enforced_on_the_mounted_mcp_surface`
  and its six siblings — the padded header, the newline-provisioned secret, and the whitespace-only
  variable failing closed.
- `packages/mcp_server_kit/tests/test_auth.py::test_a_missing_env_var_fails_closed` and
  `test_a_wrong_token_is_refused` — the middleware's own edges, in-process, unchanged.
- `tests/test_fleet.py::test_every_server_proves_its_bearer_check_against_a_running_server` — that
  every server still owes the proof.
