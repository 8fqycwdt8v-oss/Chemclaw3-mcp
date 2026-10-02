# D-2026-10-02-the-rebinding-guard-stays-on-and-is-told-the-service-name — The rebinding guard stays on and is told the Service name

**Status:** accepted · **Date:** 2026-10-02 · **Scope:** `mcp_server_kit.connector_app`, every
`servers/*/deploy/deployment.yaml`.

## What was found

- **Every server in the fleet answered `421 Misdirected Request` on `/mcp` to every in-cluster
  caller.** Measured on a kind cluster on 2026-10-02: Chemclaw3 dialled
  `http://chemclaw-mcp-safety:8859/mcp`, the short-name form its chart ships, and so did every
  other connector. Each call was refused before the bearer check, the session manager or a tool
  ran.
- **The cause is upstream's default, and nothing here set it.** `FastMCP.__init__` (mcp 1.29)
  turns DNS-rebinding protection on whenever its own `host` setting is loopback. It then admits
  only a `Host` of `127.0.0.1:*`, `localhost:*` or `[::1]:*`, plus the matching `http://` origins.
  `FastMCP("x")` defaults to `host="127.0.0.1"`, and that setting is independent of where uvicorn
  binds. Every server here constructs its `FastMCP` that way, so every server was guarded to
  loopback while listening on the pod network.
- **Nothing noticed, because nothing checked a non-loopback `Host`.** `/healthz` and `/livez` are
  plain routes outside the mounted MCP app, so every probe stayed green. Every test in this
  repository dials `http://127.0.0.1:<port>`, which sends exactly the `Host` the default admits.
- **Chemclaw3's own connector servers had the same defect.** They are being fixed on that side by
  admitting loopback plus the configured connector URL's netloc. Nothing in this repository can
  check that change.

## What was decided

- **The guard stays on, and `MCP_ALLOWED_HOSTS` adds names to its loopback defaults.**
  `mcp_server_kit/rebinding.py` builds the settings, and `connector_app` installs them before
  `streamable_http_app()` reads them. The variable is a comma-separated list of `host:port` or
  `host:*` entries, matched the way upstream matches its own patterns. Each entry also admits
  `http://<entry>` as an `Origin`, because upstream checks `Origin` whenever a request carries
  one. A server-to-server caller sends no `Origin` at all.
- **Unset means exactly upstream's loopback default.** A dev server on `127.0.0.1` behaves as it
  did before. A test compares the settings with a freshly constructed `FastMCP`'s, so a change to
  upstream's defaults turns that test red.
- **`connector_app` replaces the settings rather than merging into them.** A
  `FastMCP(host="0.0.0.0")` arrives with no guard at all, and the posture for every server is
  decided in one place.
- **An unusable entry is refused at import, naming the variable and the entry.** That covers an
  empty entry, whitespace, a URL (scheme, path or userinfo), a missing port, a port outside
  1–65535, a non-lower-case name, an unbracketed IPv6 address and any wildcard in the host, `*`
  included. A URL pasted where a host belongs would start a pod that still refuses everything. A
  `*` would start one that guards nothing. Both pods would have a green `/healthz`, which is the
  failure that hid this defect.
- **Every shipped Deployment sets the variable to its own Service's `name:port`.** The value is
  `chemclaw-mcp-<name>:<port>`, the short name Chemclaw3's chart dials. The namespace-qualified
  form is deliberately not listed. `docs/integration.md` records why that address is dropped by
  the server's own NetworkPolicy, so admitting it would admit a name nothing can reach. An operator
  who adds a name (an ingress host, a cross-namespace deployment) appends it to the same variable.
- **Every server in this repository goes through `connector_app`, so none needed separate
  treatment.** Each server's `app.py` calls it. The fleet member hosted elsewhere (`retro`) does
  not use this kit. `CLAUDE.md`'s list of what such a server owes now includes answering `/mcp` on
  the name it is dialled by, verified with that `Host`.

## Alternatives weighed

- **Disable the guard whenever the process binds a non-loopback address.** This needs no
  configuration and no Deployment change, and it is what upstream itself does for a `FastMCP`
  constructed with a non-loopback `host`. It was declined for two reasons. First, the kit cannot
  see the bind address: uvicorn is started outside `connector_app`, from a Containerfile `CMD`
  this code does not read, so the switch would key on something it would have to guess. Second,
  it gives up the one check that refuses a request addressed to a name the pod was never given.
  Inside a cluster, a DNS-rebinding attack needs a browser with network reach to the pod. The
  default-deny NetworkPolicy makes that unlikely, but the guard costs one environment variable per
  Deployment. A ratchet derives that variable from `service.yaml`, so the cost does not drift.
- **Allow any `Host` (`*`) through the variable.** This is the same as switching the guard off,
  reached through a setting that reads as a list. It is refused at startup, so taking this
  position needs a new record.
- **Derive the allow-list from the Service at runtime.** The pod has no Kubernetes API token
  (`automountServiceAccountToken: false`), and the egress guard forbids the lookup anyway.

## Revisit when

- the `mcp` SDK changes how `FastMCP` builds its transport-security defaults, or the fleet moves to
  the 2.x line (`MCPServer`), whichever comes first;
- the fleet is deployed in its own namespace, so Chemclaw3 dials a namespace-qualified name and
  every Deployment's value has to change with the NetworkPolicy peers;
- a caller needs to reach `/mcp` through an ingress or gateway that rewrites `Host`, at which point
  the entry it sends has to be argued here rather than added silently.

## What keeps it true

- `packages/mcp_server_kit/tests/test_rebinding.py::test_a_service_name_is_refused_without_the_variable`
- `packages/mcp_server_kit/tests/test_rebinding.py::test_a_listed_service_name_completes_the_handshake`
- `packages/mcp_server_kit/tests/test_rebinding.py::test_an_unlisted_host_is_still_refused`
- `packages/mcp_server_kit/tests/test_rebinding.py::test_a_foreign_origin_is_refused_even_on_a_listed_host`
- `packages/mcp_server_kit/tests/test_rebinding.py::test_an_entry_the_guard_cannot_use_is_refused_at_startup`
- `packages/mcp_server_kit/tests/test_rebinding.py::test_unset_is_exactly_upstream_s_loopback_default`
- `packages/mcp_server_kit/tests/test_rebinding.py::test_a_server_built_with_the_guard_off_is_guarded_anyway`
- `tests/test_deploy_shape.py::test_mcp_admits_the_host_its_own_service_is_dialled_by`
