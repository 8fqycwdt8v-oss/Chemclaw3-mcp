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

**`safety` needs one extra thing that is not a connector.** Chemclaw3's in-tree bundle ships
`skills/safety-screening/SKILL.md`, the judgment about which of the three tools answers which
question and how to report what comes back, and a skill is architecture layer 3 over there rather
than something a tool server can carry. This fleet's manifest therefore declares no `skills:`.
Keep that SKILL.md reachable on the Chemclaw3 side when this server wins the collision — the tools
are deterministic and have no opinion, and losing the skill loses the half that says what an empty
result does not mean.

### `calc` is not a connector Chemclaw3 dials — it is a backend behind `cached_compute`

**Do not put this fleet's `manifests/` on `CHEMCLAW_CONNECTORS_DIR` for `calc`.** That is the whole
difference between this server and the two above, and getting it wrong is silent: the name collides,
first-directory-wins applies, and Chemclaw3's own `calc` bundle would lose six tools and every
durable job to a partial port.

Chemclaw3 keeps its `calc` bundle and all fifteen of its tools. What moves is the *computation*
underneath nine of them: this server is called from inside `science/calc/store.py::cached_compute`,
as a client, on a miss.

```python
hit = await store.get(key)  # the key is an argument — so it is needed *before* the compute
if hit is not None:
    return hit.result, True
result = await compute()
```

That signature is the thing that shapes the whole integration, and it is why this server serves a
tenth tool nobody in a conversation should call:

```
identity = await remote.calculation_key(tool, arguments)   # cheap: canonicalise, embed, hash
hit      = await store.get(identity.key)                   # the four fields, ready to use
if hit is None:
    payload = await remote.<tool>(**arguments)             # the SCF, only on a miss
    await store.put(StoredResult(key=identity.key, result=payload))
```

One cheap round trip on a hit instead of a calculation; two calls on a miss, which is noise beside
minutes of CPU. The compute result carries the same `calc_key` string, so asserting it against
`identity.calc_key` is a free check that both sides agree.

Six `calc` tools have no computation to move at all, because they *are* the state — and they stay
exactly where they are, unaffected:

| Stays entirely in Chemclaw3 | What it is |
| --- | --- |
| `report_measurement`, `calculator_trust`, `calculator_outliers` | the calibration ledger — predictions reconciled against measurements |
| `find_calculations` | a query over the calculation cache |
| `list_artifacts`, `fetch_artifact` | the content-addressed artifact store |
| every `jobs:` entry | solvent screens, conformer ensembles, reaction energetics, relaxed scans, host–guest complexes, on Temporal |

**What the manifest here is for, then.** `servers/calc/connector.yaml` is this repository's own
declaration of the served surface — every tool classified, checked against the running server by
`tests/test_server.py`, and the thing a reviewer reads. It is not an instruction to register the
server as a Chemclaw3 connector, and `manifests/calc/` exists because this repository requires one
per server rather than because Chemclaw3 should point at it.

**Never derive a key on the Chemclaw3 side.** `calc_version` is assembled from the installed
`tblite` and `rdkit` distribution versions, a Hamiltonian-revision constant, an `xtb --version`
subprocess and seven pKa calibration settings — none of which a Chemclaw3 pod has after the split.
The reconstruction does not fail loudly: `xtb_cli.binary_version()` returns the literal string
`"absent"` when the binary is missing, so the string comes out well-formed, matches zero rows in
`predictions`, and `calculator_trust("pka")` reports `UNCALIBRATED` — a confident answer about a
calibration that is merely unreachable. `calculation_key` exists so that nobody has to.

**Two tools return no key, and say why.** `predict_logd` never had one — Chemclaw3 did not cache
logD, because its expensive half is already a cached pKa. `compute_thermochemistry`'s key names the
geometry its refinement loop finally settled on, which is an *output*: the loop optimises, takes a
Hessian, and displaces along the imaginary mode and repeats when the optimiser lands on a saddle.
Chemclaw3's own `compute_thermochemistry` was not a single cached calculation either — its economy
came from the nested `xtb.opt` and `xtb.hess` entries, and those are now inside one remote call.
Store its result under the `calc_key` the result itself carries; do **not** substitute the
optimisation's key, which would hit the un-refined answer wherever a row for that geometry exists.

### What the two repositories must still keep in step

Because Chemclaw3 never derives a key, the list is short — one constant:

- **`CALCULATION_EPOCH`.** It is a source constant in both repositories, folded into every
  `params_hash`, and bumped when a ChemClaw-side change makes an already-written row wrong. Chemclaw3
  still builds keys for its own in-tree calculators, so the two live in one table and must agree.
  Bump it in both repositories in the same change, or in neither.

Three things that used to be on this list are **not**, and that is the point of `calculation_key`
rather than a happy accident:

- *The calculator settings.* `CHEMCLAW_PKA_*`, `CHEMCLAW_XTB_*` and `CHEMCLAW_SOLUBILITY_RMSE_LOG`
  are interpolated into version strings — but only this server reads them, so tuning a calibration
  here changes the version everywhere it appears, consistently, with nothing to keep in sync.
- *The RDKit build.* `structure_id` is a hash of an embedded geometry, and only this server embeds.
- *The flat key format.* `calculation_key` returns the four fields, so nothing parses the string.

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

**`calc` deploys like the rest and is registered like none of them.** Same image, same NetworkPolicy,
same bearer Secret in both pods under `CHEMCLAW_CALC_TOKEN` — but its `connector.yaml` must **not**
reach `CHEMCLAW_CONNECTORS_DIR`, because Chemclaw3 addresses this server from inside `cached_compute`
rather than as a connector. If `manifests/` is mounted as a ConfigMap, mount it without `calc/`, or
copy the individual files that belong there instead.

## Failure modes worth knowing

| Symptom | Cause |
| --- | --- |
| The agent answers a solvent question from memory, with no `source` | The connector is unreachable and degraded silently. Check `/readyz`. |
| Every MCP call returns 401 | The token env var is unset or differs between the two pods. It fails closed by design. |
| The server accepts connections then hangs on the first call | The MCP session manager is not running — the mount-does-not-run-a-lifespan trap. `connector_app` handles it; a hand-rolled transport does not. |
| Startup error naming a connector | `CHEMCLAW_CONNECTORS_ENABLED` lists a name no bundle provides. That is deliberate: a typo must not silently remove a capability. |
| `calculator_trust`, `find_calculations` or a durable calc job has vanished from the surface | This fleet's `manifests/` was put on `CHEMCLAW_CONNECTORS_DIR` and its partial `calc` port won the name collision. It does not belong there: `calc` is a backend behind `cached_compute`, not a connector Chemclaw3 dials. |
| Every calculation recomputes; the cache never hits | The key was derived locally instead of read from `calculation_key`, or the two `CALCULATION_EPOCH` constants have drifted. The parts `store.get` needs come back from that tool ready to use — nothing on the Chemclaw3 side should be assembling one. |
| `calculator_trust("pka")` says `UNCALIBRATED` with n=0 on a calculator that has residuals | A `calc_version` was re-derived rather than read off the result. The ledger matches it exactly and does not pool versions, so a locally-built string — which comes out well-formed, because `binary_version()` answers `"absent"` rather than raising — matches nothing. |
| An xTB call takes minutes | Nothing is cached *on this server*. That is what `calculation_key` plus Chemclaw3's own store is for; a cold `compute_thermochemistry` is 6N+1 single points, and the manifest allows 900 s for it. |
| `connector-validate` fails on `auth` | A non-loopback URL with `mode: none`. Declare bearer. |
