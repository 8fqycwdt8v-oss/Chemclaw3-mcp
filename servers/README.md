# `servers/` — one directory per capability

Each subdirectory is a complete, independently deployable MCP server: its own dependency closure,
its own image, its own port, its own `connector.yaml`, and its own vendored data. Adding a
capability is adding a directory here.

`props` is the reference. Copy its structure — the variance between servers belongs in what they
compute, not in how they are shaped. The checklist is `docs/adding-a-server.md`; the catalogue of
what exists and what is planned is `MODULES.md`.
