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

**`calc`'s manifest is not in `manifests/`, and that is why the export line above is safe to copy.**
It lives in `manifests-internal/` beside `rxnlabel`, the other server Chemclaw3 reaches through
plain configuration rather than discovery. Getting this wrong is silent: the name collides,
first-directory-wins applies, and Chemclaw3's own `calc` bundle loses seven tools and every durable
job to a partial port, with no error at any point. Measured with Chemclaw3's own `_bundle_dirs()`
and the export line as it was published, the lost set is `report_measurement`, `find_calculations`,
`list_artifacts`, `fetch_artifact`, `calculator_trust`, `calculator_outliers` and
`compute_thermochemistry`, plus all twelve `jobs:` entries.

The directory split is the first layer. The second is in the manifest itself: it declares
`mount: backend`, and Chemclaw3's `ConnectorManifest` is `extra="forbid"`, so a deployment that
puts `manifests-internal/` on the path anyway fails at startup with a message naming the file —

```
ConnectorError: .../manifests-internal/calc/connector.yaml: invalid manifest: 1 validation error
for ConnectorManifest / mount / Extra inputs are not permitted
```

— rather than serving a reduced surface. A **connector's** manifest carries no such key, precisely
because it would do the same thing to the deployments that are supposed to mount it.

Chemclaw3 keeps its `calc` bundle and all fifteen of its tools. What moves is the *computation*
underneath them, its durable jobs included: this server is called from inside
`science/calc/store.py::cached_compute`,
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
| every `jobs:` entry | the Temporal workflows. Their *activities* now call this server's primitives; the orchestration, the retries and the durability stay put. |

### Composing a durable job out of primitives

The jobs' engines were re-cut rather than moved whole, because a composite's key names its own
output and cannot be looked up before it runs. Thermochemistry is the worked example:

```python
opt_key = await remote.calculation_key("relax_structure", {"structure": geometry})
relaxed = await cached(opt_key, lambda: remote.relax_structure(structure=geometry))
hess_key = await remote.calculation_key("compute_hessian", {"structure": relaxed["structure"]})
hessian = await cached(hess_key, lambda: remote.compute_hessian(structure=relaxed["structure"]))
thermo = rrho(hessian, relaxed["structure"], symmetry_number, temperature)  # local, pure Python
```

Both halves cache. That matters more than it looks: repeating `compute_thermochemistry` in Chemclaw3
today costs **0.007 s against 0.816 s** cold for ethanol and **0.012 s against 3.273 s** for ethyl
acetate — two orders of magnitude, entirely from the nested `xtb.opt`/`xtb.hess` entries. Shipping
the composite as one remote tool would have converted every repeat into a full recompute; composed,
every one of those hits still hits.

The same shape applies to the rest:

| Job | Composition |
| --- | --- |
| relaxed scan | `scan_point` per value — the sweep, the relative energies and the point cap are the caller's. The points were already independent: `run_scan` drives each from the input geometry rather than the previous one. |
| conformer ensemble | one `search_conformer_ensemble`, then Boltzmann populations and conformational entropy locally. |
| interaction energy | `embed_structure` ×2 → `relax_structure` ×2 → `combine_structures` → `search_binding_modes` → `relax_structure` on the best mode → subtract. Six cached rows instead of one, so changing the separation no longer re-relaxes both monomers. |
| reaction energetics / solvent screen | per-species `relax_structure` + `compute_hessian`, then stoichiometric sums. No new primitive was needed — it is composition all the way down. |

**`sample_conformers` and `compute_interaction_energy` are `expensive: true` in Chemclaw3's own
manifest, feeding `authz.expensive_actions`. That gate stays there** and is deliberately not
reproduced here: it is an authorization decision about a person, which a tool server has no basis to
make.

**The `crest` binary ships in this server's image**, which is what makes the sampling primitives —
and every Chemclaw3 composite over them — live rather than a documented intention. Chemclaw3's own
pods still have no `crest` and need none: the searches run here. A deployment that trims the binary
gets a refusal by name rather than a single-conformer answer wearing an ensemble's shape.

**One Chemclaw3 composite is built on them directly**: `microstate_pka` (the `predict_pka_ensemble`
job) is a conformer search of the neutral plus a `--deprotonate`/`--protonate` microstate search, so
the pKa it reports is a macrostate free-energy difference rather than one drawn microspecies. Both
halves are ordinary cached primitives here; the composition, the calibration and the warnings are
Chemclaw3's, exactly as the split requires.

**What the manifest here is for, then.** `servers/calc/connector.yaml` is this repository's own
declaration of the served surface — every tool classified, checked against the running server by
`tests/test_server.py`, and the thing a reviewer reads. It is not an instruction to register the
server as a Chemclaw3 connector, and `manifests-internal/calc/` exists because this repository
requires one registration per server rather than because Chemclaw3 should point at it.

**Never derive a key on the Chemclaw3 side.** `calc_version` is assembled from the installed
`tblite` and `rdkit` distribution versions, a Hamiltonian-revision constant, an `xtb --version`
subprocess and seven pKa calibration settings — none of which a Chemclaw3 pod has after the split.
The reconstruction does not fail loudly: `xtb_cli.binary_version()` returns the literal string
`"absent"` when the binary is missing, so the string comes out well-formed, matches zero rows in
`predictions`, and `calculator_trust("pka")` reports `UNCALIBRATED` — a confident answer about a
calibration that is merely unreachable. `calculation_key` exists so that nobody has to.

**One tool returns no key, and says why.** `predict_logd` never had one — Chemclaw3 did not cache
logD, because its expensive half is already a cached pKa, and the caveat names that pKa's key.

**The two CREST searches refuse to be keyed without their binary**, on purpose:
`CrestSpec.calc_version()` answers `crest-absent` rather than raising, so a key *is* derivable with
no crest and would name a program that cannot run. The probe refuses exactly where the search would.

### What the two repositories must still keep in step

Because Chemclaw3 never derives a key, the list is empty — and the one constant everybody expects to
be on it is not:

- **`CALCULATION_EPOCH`.** A source constant in both repositories, folded into every `params_hash`,
  bumped when a ChemClaw-side change makes an already-written row wrong.

  **The two compose; they are not compared, and the older claim that they "must match" rested on a
  premise that has since gone.** That premise was that Chemclaw3 still builds keys for its own
  in-tree calculators. It does not: `CalculationKey.build` has **no caller left** in its `src/`, and
  `cached_compute` has exactly one — `connectors/calc/remote.py::cached_remote`. Every row in
  `calculation_results` is now keyed by `remote_key`, which rebuilds this server's four fields and
  folds *its* epoch over **our** `params_hash`:
  `stable_hash({"epoch": <theirs>, "remote_params": <ours>})`. So a bump on either side alone
  changes the composed digest and misses every stored row, which is exactly what an epoch is for.

  Move them together anyway — it keeps the two epoch logs describing the same events — but as a
  convention, not as a correctness invariant, and knowing that a unilateral bump costs CPU rather
  than serving a stale row. `servers/calc/tests/test_key_contract.py` pins what a divergence would
  actually break: the pure `stable_hash`, the `{"epoch": ..., "params": ...}` envelope, the flat
  string format, and the four field *names* `remote_key` reads.

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

- a rootless image built from `servers/<name>/Containerfile`, **built with
  `--build-arg CHEMCLAW_REVISION=$(git rev-parse HEAD)`** — see below;
- `servers/<name>/deploy/networkpolicy.yaml` — default-deny egress, ingress from the Chemclaw3 pod
  and the Prometheus scraper only;
- the bearer token as a plain Secret mounted into **both** pods under the same variable name
  (`CHEMCLAW_PROPS_TOKEN` for `props`): Chemclaw3 reads it to send, the server reads it to verify.

### The revision is a build argument, and forgetting it is silent

Chemclaw3's audit row records the *orchestrator's* commit. Since the chemistry moved here, the
process that computed a number ships on this repository's cadence instead, and that column no longer
names it. So `MCP_SERVER_REVISION` rides the `initialize()` handshake: `connector_app` stamps it onto
`serverInfo.version`, which every client already reads when it opens a session, and onto `/healthz`
for a probe that has no session. No extra endpoint, no extra round trip, no field on every result.

The build argument is the whole supply chain, and an image built without it answers `"unknown"`
rather than failing — which is precisely how Chemclaw3's own revision field stayed empty for eight
months with its function, its column and its test all present and correct. `tests/test_fleet.py`
therefore asserts the `ARG`/`ENV` pair in each Containerfile, not merely that the server reads the
variable. A pipeline that drops the `--build-arg` is the one remaining way to get `"unknown"`, and
it is visible: `curl .../healthz` says so.

The manifest directory has to be readable by the Chemclaw3 pod. Either mount `manifests/` as a
ConfigMap and prepend it to `CHEMCLAW_CONNECTORS_DIR`, or copy the `connector.yaml` files into
Chemclaw3's own image at build time. The ConfigMap route keeps the two release cycles independent,
which is the point of this repository existing separately. Mount `manifests/` and nothing else:
`manifests-internal/` is not a second path to add, it is the directory whose contents must not be
discovered.

**`calc` and `rxnlabel` deploy like the rest and are registered like none of them.** Same images,
same NetworkPolicies, same bearer Secrets in both pods (`CHEMCLAW_CALC_TOKEN`,
`CHEMCLAW_RXNLABEL_TOKEN`) — but their manifests must never reach `CHEMCLAW_CONNECTORS_DIR`, because
Chemclaw3 addresses the first from inside `cached_compute` and the second from a background drain.
Keeping them out of `manifests/` is what makes "mount the whole directory" the correct instruction
instead of a trap with a footnote.

## Failure modes worth knowing

| Symptom | Cause |
| --- | --- |
| The agent answers a solvent question from memory, with no `source` | The connector is unreachable and degraded silently. Check `/readyz`. |
| Every MCP call returns 401 | The token env var is unset or differs between the two pods. It fails closed by design. |
| The server accepts connections then hangs on the first call | The MCP session manager is not running — the mount-does-not-run-a-lifespan trap. `connector_app` handles it; a hand-rolled transport does not. |
| Startup error naming a connector | `CHEMCLAW_CONNECTORS_ENABLED` lists a name no bundle provides. That is deliberate: a typo must not silently remove a capability. |
| `calculator_trust`, `find_calculations` or a durable calc job has vanished from the surface | A `calc` manifest from this fleet reached `CHEMCLAW_CONNECTORS_DIR` and its partial port won the name collision. It cannot come from `manifests/` any more — check for a hand-copied `connector.yaml`, or a path pointing into `manifests-internal/`. |
| Chemclaw3 refuses to start with `invalid manifest: ... mount ... Extra inputs are not permitted` | `manifests-internal/` is on `CHEMCLAW_CONNECTORS_DIR`. That is the guard working: those servers are addressed by configuration (`CHEMCLAW_CALC_SERVER_URL`, `rxnlabel_server_url`), never discovered. Remove the path. |
| Every calculation recomputes; the cache never hits | The key was derived locally instead of read from `calculation_key`, or a `CALCULATION_EPOCH` was bumped on either side (which invalidates every row deliberately — the two compose). The parts `store.get` needs come back from that tool ready to use; nothing on the Chemclaw3 side should be assembling one. |
| `calculator_trust("pka")` says `UNCALIBRATED` with n=0 on a calculator that has residuals | A `calc_version` was re-derived rather than read off the result. The ledger matches it exactly and does not pool versions, so a locally-built string — which comes out well-formed, because `binary_version()` answers `"absent"` rather than raising — matches nothing. |
| An xTB call takes minutes | Nothing is cached *on this server*. That is what `calculation_key` plus Chemclaw3's own store is for; a cold `compute_thermochemistry` is 6N+1 single points, and the manifest allows 900 s for it. |
| `connector-validate` fails on `auth` | A non-loopback URL with `mode: none`. Declare bearer. |
