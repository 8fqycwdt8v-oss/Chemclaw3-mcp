# `packages/`

Shared libraries, not capabilities. Nothing here is served to the agent.

- [`mcp_server_kit/`](mcp_server_kit/) — the shape every server in this repository has: the FastAPI
  transport, bearer auth, identity logging, vendored-dataset loading, and the egress guard.

A server's chemistry never lives here. If two servers need the same computation, that is a library
in its own right, not a shared corner of the transport kit.
