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
| [`MODULES.md`](MODULES.md) | The catalogue — 19 servers across five tranches, their tools, their data, and the port registry. |
| [`CLAUDE.md`](CLAUDE.md) | The conventions every server follows, and the reasons behind them. |
| [`servers/props/`](servers/props/) | The reference server: solvent and pure-component properties. Copy this one. |
| [`packages/mcp_server_kit/`](packages/mcp_server_kit/) | The shared shape: FastAPI transport, bearer auth, identity logging, vendored datasets, the egress guard. |
| [`manifests/`](manifests/) | One directory per server, holding its `connector.yaml`. Point `CHEMCLAW_CONNECTORS_DIR` here. |
| [`docs/integration.md`](docs/integration.md) | Wiring a Chemclaw3 checkout to this fleet. |
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
curl -s localhost:8850/healthz            # {"status":"ok","server":"props"}
curl -si localhost:8850/mcp | head -1     # HTTP/1.1 401 Unauthorized — the bearer check
```

`make offline-run` runs the same suite inside a network namespace with no route off the host. It is
the strongest form of the no-egress claim, because it does not trust this repository's own code:
it takes the network away and checks every answer is unchanged.

## Wiring it to Chemclaw3

Two environment variables, no code change:

```sh
export CHEMCLAW_CONNECTORS_DIR="/path/to/Chemclaw3-mcp/manifests:<chemclaw's own connectors dir>"
export CHEMCLAW_CONNECTOR_URLS='{"props":"http://127.0.0.1:8850/mcp"}'
export CHEMCLAW_PROPS_TOKEN=dev-token     # the same variable both sides read
```

Then, in the Chemclaw3 checkout, `make connector-validate` resolves the manifest and the front door
picks the tools up on the next turn. Full instructions, including the Helm side and the
degrades-silently failure mode to watch for, are in [`docs/integration.md`](docs/integration.md).

**One server is wired differently and it is not optional to know which.** `calc` is not a connector
Chemclaw3 dials: it serves the *computation* behind nine of that bundle's fifteen tools and is called
from inside Chemclaw3's own `cached_compute` on a cache miss. Putting its manifest on
`CHEMCLAW_CONNECTORS_DIR` would let a partial port win the `calc` name collision and take the
calibration ledger, the calculation cache, the artifact store and every durable calc job off the
agent's surface — with no error. See [`servers/calc/README.md`](servers/calc/README.md).

## Adding a server

Read [`CLAUDE.md`](CLAUDE.md) first, then follow
[`docs/adding-a-server.md`](docs/adding-a-server.md). In short: a directory under `servers/`, three
layers inside it (`engine/` ← `tools.py` ← `app.py`), a vendored dataset with its licence and
checksum, a `connector.yaml` symlinked into `manifests/`, a NetworkPolicy denying egress, and the
tests that hold all of those to each other.
