# `mcp_server_kit` — the shape every server here has

Written once so a server's `app.py` is three lines, and so the cross-cutting behaviours cannot be
forgotten one server at a time. Nothing in here is domain logic; a server's chemistry lives in its
own `engine/`.

| Module | What it is |
| --- | --- |
| `app.py` | `connector_app()` — the FastAPI app: `/healthz`, `/metrics`, mounted `/mcp`, the session-manager lifespan, per-tool-call caller binding and trace continuation, and tool-error sanitising. |
| `auth.py` | Bearer check on `/mcp` (probes stay open, comparison in bytes, fails closed), the caller log, and the request-body cap. |
| `identity.py` | The `X-Chemclaw-*` headers and the contextvars that carry them. Provenance, never authorization. |
| `tracing.py` | The receiving half of Chemclaw3's `traceparent`: one span per tool call, under the caller's trace. Off unless `MCP_TRACING_ENABLED` says otherwise, and it constructs no exporter — the `otel` extra installs the API only. |
| `datasets.py` | Vendored-corpus loading: all six provenance fields required, checksum verified on load. |
| `egress.py` | The runtime guard. Armed on import; a non-loopback `connect`, `sendto`/`sendmsg` or DNS lookup raises `EgressForbidden`. A child process and a `ctypes` call are outside it by construction — `make offline-run` is what covers those. |
| `no_egress.py` | The static scan — AST, not grep — that each server's `test_no_egress.py` calls. |
| `testing.py` | A real MCP session against a running server, and the manifest↔served-tools assertion. |

## The parts that exist because they went wrong somewhere

Most of this is ported in shape from Chemclaw3's `chemclaw/connectors/server.py`, minus its
`chemclaw.core` dependencies. The behaviours below are here specifically because their absence was
quiet — no count, because the one written here said four over five bullets before this list grew:

- **Running the MCP session manager from the parent lifespan.** Mounting a Starlette app does not
  run its lifespan; the server then accepts connections and hangs on the first request.
- **Re-binding the caller per tool call.** A tool body runs in the session manager's task, so
  middleware-bound identity is the handshake's. Measured in Chemclaw3: alice's handshake then bob's
  call had the tool reading alice.
- **Checking the bearer token on the serving side.** Chemclaw3 shipped `BearerAuth` on the sending
  side only — a deployment mounted a secret, recorded the control as enabled, and served every tool
  to anything that could reach the pod.
- **Continuing the trace, per tool call.** Chemclaw3 injects W3C `traceparent` on every connector
  call and its tracing docstring says the consequence in the present tense — "a connector's work
  appears inside the turn that asked for it". That was true only of the connectors Chemclaw3 hosts
  itself: every server in this fleet read the identity headers beside it and dropped the trace
  context, so a CREST search produced no span at all on this side. Per tool call for the same reason
  the caller is bound there — one MCP session carries many calls, and a span parented on the
  handshake would put all of them inside whichever turn opened the connection. That last sentence
  is a claim a test now holds: `test_tracing.py` sends two calls down one session with two
  different traces, because every other test there opens a session, sends one call, and would pass
  against a handshake-scoped implementation. The tool name on the span takes the clamp `/metrics`
  takes, for the same cardinality reason one hop further out.
- **Refusing an oversized body before a handler reads it.** The counter alone bounds only what
  somebody *reads*: a route that ignores the body never pulls from the receive channel, so an
  oversized request to it was served 200 with the cap installed and silent. The declared
  `content-length` is now refused up front, and the running total still guards the chunked case —
  by reporting a **disconnect** on the receive channel rather than raising. It raised for a while,
  and the exception could not survive the two `BaseHTTPMiddleware` layers between the cap and the
  app: a chunked oversize body got 500 with a per-request traceback, so the counter delivered a 413
  in no configuration at all while its only test exercised the `content-length` path.
- **Passing upstream's own `Unknown tool: x` through the sanitiser.** `ToolManager` raises it with
  no `__cause__`, which made it indistinguishable from an internal fault: a model that guessed a
  stale tool name was told "an internal error occurred", and an ERROR-level traceback fired for a
  client input error.

Most of those are only visible against a *running* server, which is what
`tests/test_connector_app.py` is for; the trace continuation is checked the same way, against a real socket, in `tests/test_tracing.py`.

`tests/` covers each of them. **This package used to be exempt from the `no_egress` scan as a
whole**, for the reason that is real but belongs to one file: `egress.py` has to import `socket` to
patch it. Granting it to the package rather than to the file is precisely the shape `no_egress.py`
rejects for a server, and it left the most widely installed code in the repository unscanned —
`testing.py` imports `httpx`, which was a dependency of nothing. `tests/test_no_egress.py` now scans
`src/mcp_server_kit` with three named exemptions, each with the test it owes, and records what the
scan cannot buy: `import mcp_server_kit` reaches `httpx` through `mcp.shared.session` regardless, so
the control that stops an outbound call is the runtime guard and the NetworkPolicy, not the absence
of a client.
