# Wiring a Chemclaw3 checkout to this fleet

Everything below is Chemclaw3's own mechanism, used as intended. No fork, no patch, no core edit.

## The contract, in one table

| Fact | Where it lives in Chemclaw3 |
| --- | --- |
| A bundle is any subdirectory of `connectors_dir` containing `connector.yaml`. `CHEMCLAW_CONNECTORS_DIR` is a `PATH`-style list; earlier directories win a name collision | `src/chemclaw/core/config/connectors.py` |
| Discovery is not enablement. `CHEMCLAW_CONNECTORS_ENABLED` narrows the set *and fixes its order* — tool order is part of the prompt | same file |
| A name listed there that no bundle provides is a **startup error**, not a silently missing capability | same file |
| Per-connector address override: `CHEMCLAW_CONNECTOR_URLS`, a JSON map. In Helm, `connectors.<name>.url` | `D-2026-08-09-a-connector-we-do-not-run` |
| Setting `connectors.<name>.url` is what says "this server is not ours to run": that bundle gets no Deployment and no Service | same ADR |
| A non-loopback `url` with `auth: {mode: none}` is refused by the manifest model | `connectors/manifest.py::HttpEndpoint` |
| Every tool must be classified exactly once as `read_only` or `state_changing` | `connectors/manifest.py`, D-167 |
| `make connector-validate` resolves every manifest against live code | Chemclaw3's `Makefile` |
| **An unreachable connector degrades silently** — its tools vanish from the turn and the model reasons from what remains | `connectors/transport.py` |

That last row is the one to plan around. A server that is down does not produce an error a chemist
will see; it produces an answer with less evidence behind it. Chemclaw3 reports it through
`/readyz` and the `chemclaw_connectors_unhealthy` gauge, and `CHEMCLAW_CONNECTORS_REQUIRED=true`
turns it into a hard failure — which is the right setting for a GxP deployment.

## Local development

Run the server:

```sh
cd /path/to/Chemclaw3-mcp
make run-props                            # 127.0.0.1:8850, token defaults to `dev-token`
```

Point Chemclaw3 at it. `CHEMCLAW_CONNECTORS_DIR` must keep Chemclaw3's own directory on the path,
or the shipped bundles disappear:

```sh
cd /path/to/Chemclaw3
CHEMCLAW_OWN=$(uv run python -c "import chemclaw.connectors, pathlib; print(pathlib.Path(chemclaw.connectors.__file__).parent)")

export CHEMCLAW_CONNECTORS_DIR="/path/to/Chemclaw3-mcp/manifests:$CHEMCLAW_OWN"
export CHEMCLAW_CONNECTOR_URLS='{"props":"http://127.0.0.1:8850/mcp"}'
export CHEMCLAW_PROPS_TOKEN=dev-token     # the same variable the server verifies

make connector-validate                   # the manifest resolves and is classified
uvicorn chemclaw.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

Then ask the agent something only this server can answer — *"what is the flash point of 2-MeTHF,
and what could replace dichloromethane in a plant?"* — and confirm the tool was **called**, not
recalled. The `source` field in the answer is the tell: it names the vendored dataset and its
licence.

### The servers that collide with a Chemclaw3 bundle, on purpose

`chem` and `safety` are **ports** of Chemclaw3's own in-tree connectors and carry the same names, so
which of each pair answers is decided by the order of `CHEMCLAW_CONNECTORS_DIR` — first directory
wins (`connectors/registry.py::_bundle_dirs`). Putting this fleet's `manifests/` first, as above, is
what makes these servers the `chem` and the `safety` the agent sees:

```sh
export CHEMCLAW_CONNECTORS_DIR="/path/to/Chemclaw3-mcp/manifests:$CHEMCLAW_OWN"   # this one wins
export CHEMCLAW_CONNECTOR_URLS='{"chem":"http://127.0.0.1:8858/mcp","safety":"http://127.0.0.1:8859/mcp"}'
export CHEMCLAW_CHEM_TOKEN=dev-token      # the same variables the servers verify
export CHEMCLAW_SAFETY_TOKEN=dev-token
```

Reverse the two paths and the in-tree bundles win instead, with no error either way — which is the
intended behaviour, and the thing to check when a `chem` or `safety` answer arrives that these
servers cannot have produced. There is no configuration in which both are reachable: one name, one
URL key, one winner.

### `calc` collides the same way and must be wired the *opposite* way round

**`servers/calc/` is not a complete port of Chemclaw3's `calc` bundle**, and the difference decides
the configuration. Nine of that bundle's fifteen tools moved; six could not, because they *are* the
state this fleet's servers do not have:

| Only Chemclaw3 serves it | What it is |
| --- | --- |
| `report_measurement`, `calculator_trust`, `calculator_outliers` | the calibration ledger — predictions reconciled against measurements |
| `find_calculations` | a query over the calculation cache |
| `list_artifacts`, `fetch_artifact` | the content-addressed artifact store |
| every `jobs:` entry | solvent screens, conformer ensembles, reaction energetics, relaxed scans, host–guest complexes, on Temporal |

The name is still `calc`, so first-directory-wins applies exactly as above — and putting this
fleet's `manifests/` first would remove all six of those and every durable calc job from the agent's
surface, **with no error**. So for `calc` the supported order is the reverse:

```sh
export CHEMCLAW_CONNECTORS_DIR="$CHEMCLAW_OWN:/path/to/Chemclaw3-mcp/manifests"   # in-tree wins
```

That keeps the full `calc` bundle and gives up this server for it, which is the right default. The
two orders cannot be mixed within one process — the path is global, not per connector — so a
deployment that wants this fleet's `chem` and `safety` *and* Chemclaw3's full `calc` copies the
individual `connector.yaml` files it wants into one directory rather than chaining the two.

**Running compute-only, deliberately.** Put this fleet first and the nine tools answer from
`servers/calc/` instead:

```sh
export CHEMCLAW_CONNECTORS_DIR="/path/to/Chemclaw3-mcp/manifests:$CHEMCLAW_OWN"
export CHEMCLAW_CONNECTOR_URLS='{"calc":"http://127.0.0.1:8860/mcp"}'
export CHEMCLAW_CALC_TOKEN=dev-token
```

What that costs is above; what it buys is a calculator that scales on its own and takes `tblite` out
of the chat service's image. **What it does not cost is the ability to cache or calibrate the
results**, and that is the deliberate part of the design: every result carries `calc_version`, and
eight of the nine carry `calc_key` — the full `calc_type@calc_version:input_hash:params_hash` string
the calculation *would* be stored under. A caller holding those can write the cache row and the
ledger row itself.

**Do not re-derive either on the Chemclaw3 side.** Both are assembled from the installed `tblite`
and `rdkit` distribution versions, a Hamiltonian-revision constant, an `xtb --version` subprocess
and seven pKa calibration settings — none of which a Chemclaw3 pod has after the split. The
reconstruction does not fail loudly: `xtb_cli.binary_version()` returns the literal string
`"absent"` when the binary is missing, so the string comes out well-formed, matches zero rows in
`predictions`, and `calculator_trust("pka")` reports `UNCALIBRATED` — a confident answer about a
calibration that is merely unreachable.

**One environment variable set is shared by both sides and has to match.** `servers/calc` reads
Chemclaw3's own `CHEMCLAW_*` calculator settings under Chemclaw3's own field names — notably
`CHEMCLAW_XTB_METHOD`, `CHEMCLAW_XTB_ENGINE`, `CHEMCLAW_PKA_SOLVENT`, the four
`CHEMCLAW_PKA_*_CALIBRATION_*` constants, `CHEMCLAW_PKA_UNCERTAINTY`,
`CHEMCLAW_PKA_BASE_UNCERTAINTY` and `CHEMCLAW_SOLUBILITY_RMSE_LOG` — because those values are
*inside* the version strings. Tune one on one side only and the ledger rows stop meeting.

**`safety` needs one extra thing that is not a connector.** Chemclaw3's in-tree bundle ships
`skills/safety-screening/SKILL.md`, the judgment about which of the three tools answers which
question and how to report what comes back, and a skill is architecture layer 3 over there rather
than something a tool server can carry. This fleet's manifest therefore declares no `skills:`.
Keep that SKILL.md reachable on the Chemclaw3 side when this server wins the collision — the tools
are deterministic and have no opinion, and losing the skill loses the half that says what an empty
result does not mean.

### Checking it is really connected

```sh
curl -s localhost:8000/readyz             # connectors reported here
curl -s localhost:8000/metrics | grep chemclaw_connectors_unhealthy
```

Absence of an error is not success. Check one of these two.

## Deployment (OpenShift / Helm)

On Chemclaw3's side, one value says the server is hosted elsewhere and gives the address:

```yaml
connectors:
  props:
    enabled: true
    url: http://chemclaw-mcp-props.chemclaw-tools.svc:8850/mcp
```

Presence of `url` is the flag — there is no separate `external: true`, deliberately, because a
boolean and an address are two declarations of one fact that can disagree. That bundle then gets no
app Deployment and no Service from Chemclaw3's chart.

On this side, each server ships:

- a rootless image built from `servers/<name>/Containerfile`;
- `servers/<name>/deploy/networkpolicy.yaml` — default-deny egress, ingress from the Chemclaw3 pod
  and the Prometheus scraper only;
- the bearer token as a plain Secret mounted into **both** pods under the same variable name
  (`CHEMCLAW_PROPS_TOKEN` for `props`): Chemclaw3 reads it to send, the server reads it to verify.

The manifest directory has to be readable by the Chemclaw3 pod. Either mount `manifests/` as a
ConfigMap and prepend it to `CHEMCLAW_CONNECTORS_DIR`, or copy the `connector.yaml` files into
Chemclaw3's own image at build time. The ConfigMap route keeps the two release cycles independent,
which is the point of this repository existing separately.

## Failure modes worth knowing

| Symptom | Cause |
| --- | --- |
| The agent answers a solvent question from memory, with no `source` | The connector is unreachable and degraded silently. Check `/readyz`. |
| Every MCP call returns 401 | The token env var is unset or differs between the two pods. It fails closed by design. |
| The server accepts connections then hangs on the first call | The MCP session manager is not running — the mount-does-not-run-a-lifespan trap. `connector_app` handles it; a hand-rolled transport does not. |
| Startup error naming a connector | `CHEMCLAW_CONNECTORS_ENABLED` lists a name no bundle provides. That is deliberate: a typo must not silently remove a capability. |
| `calculator_trust`, `find_calculations` or a durable calc job has vanished from the surface | This fleet's `manifests/` is ahead of Chemclaw3's own directory and its partial `calc` port won the name. Put the in-tree directory first — see above. |
| `calculator_trust("pka")` says `UNCALIBRATED` with n=0 on a calculator that has residuals | A `calc_version` was re-derived instead of read off the result, or the two sides' `CHEMCLAW_PKA_*` settings differ. The ledger matches the version exactly and does not pool. |
| An xTB tool call times out | Nothing is cached on this server. A cold `compute_thermochemistry` is 6N+1 single points; the manifest allows 900 s for that reason. |
| `connector-validate` fails on `auth` | A non-loopback URL with `mode: none`. Declare bearer. |
