# `mcp_server_kit` — the shape every server here has

Written once so a server's `app.py` is three lines, and so the cross-cutting behaviours cannot be
forgotten one server at a time. Nothing in here is domain logic; a server's chemistry lives in its
own `engine/`.

| Module | What it is |
| --- | --- |
| `app.py` | `connector_app()` — the FastAPI app: `/healthz`, `/metrics`, mounted `/mcp`, the session-manager lifespan, per-tool-call caller binding, and tool-error sanitising. |
| `auth.py` | Bearer check on `/mcp` (probes stay open, comparison in bytes, fails closed), the caller log, and the request-body cap. |
| `identity.py` | The `X-Chemclaw-*` headers and the contextvars that carry them. Provenance, never authorization. |
| `datasets.py` | Vendored-corpus loading: all six provenance fields required, checksum verified on load. |
| `egress.py` | The runtime guard. Armed on import; a non-loopback `connect` raises `EgressForbidden`. |
| `no_egress.py` | The static scan — AST, not grep — that each server's `test_no_egress.py` calls. |
| `testing.py` | A real MCP session against a running server, and the manifest↔served-tools assertion. |

## The parts that exist because they went wrong somewhere

Most of this is ported in shape from Chemclaw3's `chemclaw/connectors/server.py`, minus its
`chemclaw.core` dependencies. Four behaviours are here specifically because their absence was quiet:

- **Running the MCP session manager from the parent lifespan.** Mounting a Starlette app does not
  run its lifespan; the server then accepts connections and hangs on the first request.
- **Re-binding the caller per tool call.** A tool body runs in the session manager's task, so
  middleware-bound identity is the handshake's. Measured in Chemclaw3: alice's handshake then bob's
  call had the tool reading alice.
- **Checking the bearer token on the serving side.** Chemclaw3 shipped `BearerAuth` on the sending
  side only — a deployment mounted a secret, recorded the control as enabled, and served every tool
  to anything that could reach the pod.
- **Refusing an oversized body before a handler reads it.** The counter alone bounds only what
  somebody *reads*: a route that ignores the body never pulls from the receive channel, so an
  oversized request to it was served 200 with the cap installed and silent. The declared
  `content-length` is now refused up front, and the running total still guards the chunked case.

`tests/` covers each of them. This package's own modules are exempt from the `no_egress` scan for
the obvious reason — `egress.py` has to import `socket` to guard it.
