# Chemclaw3-mcp

MCP tool servers for [`Chemclaw3`](https://github.com/8fqycwdt8v-oss/Chemclaw3), the agent for
pharmaceutical and chemical process R&D.

Chemclaw3 holds the orchestration, the knowledge graph and its own first-party capabilities. This
repository holds the tools it does not run itself: **one capability per server, one server per
process, each with the `connector.yaml` that registers it**. Chemclaw3 picks a server up with no
code change on its side — a manifest directory and an address.

Every server answers from data baked into its image or mounted read-only. **None of them makes an
outbound call at request time, in any environment** — see [`CLAUDE.md`](CLAUDE.md) for how that is
enforced rather than requested.

## What is here

| | |
| --- | --- |
| [`MODULES.md`](MODULES.md) | The catalogue — every server this fleet has built or proposed, grouped in tranches, with their tools, their data, and the port registry. |
| [`CLAUDE.md`](CLAUDE.md) | The conventions every server follows, and the reasons behind them. |
| [`servers/props/`](servers/props/) | The reference server: solvent and pure-component properties. Copy this one. |
| [`packages/mcp_server_kit/`](packages/mcp_server_kit/) | The shared shape: FastAPI transport, bearer auth, identity logging, vendored datasets, the egress guard. |
| [`manifests/`](manifests/) | One directory per **connector**, holding its `connector.yaml`. Point `CHEMCLAW_CONNECTORS_DIR` here — and only here. |
| [`manifests-internal/`](manifests-internal/) | The two servers Chemclaw3 must not discover — `calc` and `rxnlabel`. Reached by configuration, never mounted. |
| [`docs/operations.md`](docs/operations.md) | **Running it**: build an image, deploy a server, wire it into Chemclaw3, verify, troubleshoot. |
| [`docs/integration.md`](docs/integration.md) | Wiring a Chemclaw3 checkout to this fleet, in depth — including the `calc` cache seam. |
| [`docs/delivery.md`](docs/delivery.md) | The Jenkins pipeline that builds, verifies and publishes the images. |
| [`docs/adding-a-server.md`](docs/adding-a-server.md) | The checklist for a new server. |

## Quickstart

Prerequisites: Python ≥ 3.11 and [`uv`](https://docs.astral.sh/uv/).

```sh
uv sync                  # install the workspace: the kit, every server, and dev dependencies
make check               # lint + mypy --strict + the whole suite — what CI runs
make run-props           # the reference server on 127.0.0.1:8850
```

With the server running:

```sh
curl -s localhost:8850/healthz            # {"status":"ok","server":"props","revision":…,"bounds":{…},"datasets":["process-solvents@0.2.0"]}
curl -s localhost:8850/livez              # {"status":"alive",…}
curl -si localhost:8850/mcp | head -1     # HTTP/1.1 401 Unauthorized — the bearer check
```

Every server has a `make run-<name>` target on its own port with a `dev-token` default; a full MCP
tool call by `curl` is in [`docs/operations.md`](docs/operations.md#4-verify).

`make offline-run` runs the same suite inside a network namespace with no route off the host. It is
the strongest form of the no-egress claim, because it does not trust this repository's own code:
it takes the network away and checks every answer is unchanged. It is the **only** cover for the
egress channels no static scan reaches — a child process, and a `ctypes` call into libc — so
`make check` runs it too wherever the kernel allows an unprivileged network namespace, and names it
on screen where it does not.

## Running it in a cluster

[`docs/operations.md`](docs/operations.md) is the runbook: building an image (`podman build -f
servers/<name>/Containerfile --build-arg CHEMCLAW_REVISION=$(git rev-parse HEAD) .` from the
repository root), applying `servers/<name>/deploy/`, the key the shipped Deployment reads its
bearer from (`chemclaw-secrets`, the Secret Chemclaw3's chart reads), pinning the placeholder image
to a digest, every environment variable with its default, how to verify a pod, and a
troubleshooting table for the failures that look like something else (a `421` from the
DNS-rebinding guard, a `401` from a mismatched token, a readiness `503` naming a corpus checksum).

## Wiring it to Chemclaw3

No code change on either side. Chemclaw3 already ships a `connector.yaml` for `chem`, `safety`,
`rxnpredict`, `props`, `thermalsafety`, `kinetics`, `unitops` and `suitability`, so for those it needs
only an address and the token, under the same variable name the server verifies:

```sh
export CHEMCLAW_CONNECTOR_URLS='{"props":"http://127.0.0.1:8850/mcp"}'   # Helm: connectors.<name>.url
export CHEMCLAW_PROPS_TOKEN=dev-token                                     # the same variable both sides read
```

`pyexec` is the connector Chemclaw3 does not declare: its manifest comes from this repository's
[`manifests/`](manifests/), prepended to `CHEMCLAW_CONNECTORS_DIR` (Helm: `extraConnectors`).
Full instructions, including the five connectors that ship disabled and the degrades-silently failure
mode to watch for, are in [`docs/operations.md`](docs/operations.md#3-wire-it-into-chemclaw3) and
[`docs/integration.md`](docs/integration.md).

**Two servers are not connectors at all.** `calc` holds the *physics* behind Chemclaw3's own `calc`
bundle and is called from inside its `cached_compute` on a cache miss (`CHEMCLAW_CALC_SERVER_URL`);
`rxnlabel` is called by a background corpus drain (`CHEMCLAW_RXNLABEL_SERVER_URL`). Mounting
`calc`'s manifest would let a partial surface win the `calc` name collision and take the calibration
ledger, the calculation cache, the artifact store and every durable calc job off the agent's
surface — with no error. So their manifests live in [`manifests-internal/`](manifests-internal/),
which nothing tells you to mount, and each declares `mount: backend` — a key Chemclaw3's manifest
model refuses, so pointing a path there anyway is a startup error naming the file rather than a
silent swap.

`calc` is also the server that shows what this fleet does and does not promise: it may run for
hours, and it may not hold state. See [`servers/calc/README.md`](servers/calc/README.md).

## Adding a server

Read [`CLAUDE.md`](CLAUDE.md) first, then follow
[`docs/adding-a-server.md`](docs/adding-a-server.md). In short: a directory under `servers/`, three
layers inside it (`engine/` ← `tools.py` ← `app.py`), a vendored dataset with its licence and
checksum, a `connector.yaml` symlinked into `manifests/`, a NetworkPolicy denying egress, and the
tests that hold all of those to each other.
