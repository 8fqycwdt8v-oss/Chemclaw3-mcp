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

## What Chemclaw3 already declares

Chemclaw3's image ships a `connector.yaml` for `chem`, `safety`, `rxnpredict`, `props`,
`thermalsafety`, `kinetics`, `unitops` and `suitability`, each describing the server in **this**
repository — same name, same tools, same `token_env` — so that its validators can resolve tool names
and its skills can name them. Its comments call them declarations rather than servers: Chemclaw3
runs none of them. For those, wiring is an address and a token; this repository's `manifests/` does
not need to be mounted at all.

The last five ship `default_enabled: false` there, because every bound connector's tool schemas are
paid on every model call. An empty `CHEMCLAW_CONNECTORS_ENABLED` binds none of them; an explicit list
binds exactly what it names, in that order, and replaces the default set — so a list that adds
`props` has to name the rest of the surface too.

`pyexec` is the one connector Chemclaw3 does not declare, so its manifest has to come from
`manifests/`. `calc` and `rxnlabel` are not connectors at all (below).

**When two manifests carry one name, the first directory on `CHEMCLAW_CONNECTORS_DIR` wins the tool
surface outright** (`connectors/registry.py::_bundle_dirs`) — no merge, no warning. Prepending this
repository's `manifests/` therefore makes *this* repository's copy authoritative for every name in
it, which is useful for testing a tool-surface change before Chemclaw3's copy follows, and has one
consequence to know about: of the opt-in servers only `pyexec`'s manifest here carries
`default_enabled: false`, so with an empty `CHEMCLAW_CONNECTORS_ENABLED` the other five become
enabled by default once their copy here wins the name. Chemclaw3 records that difference as argued
(`tests/test_sibling_manifest_agreement.py::_ARGUED_DIVERGENCES`), so closing it is a change both
repositories make together. A bundle's `skills/` and `profiles/` are merged
from every directory carrying the name, winner first (`registry._bundle_content_dirs`), so
Chemclaw3's `safety-screening` skill survives whichever `safety` manifest wins. Chemclaw3's
`tests/test_sibling_manifest_agreement.py` compares the two copies' bundle-level keys.

## Local development

Run the server:

```sh
cd /path/to/Chemclaw3-mcp
make run-props                            # 127.0.0.1:8850, token defaults to `dev-token`
```

Point Chemclaw3 at it. Chemclaw3's own `props` declaration already names `http://127.0.0.1:8850/mcp`
(every declaration's loopback default is this fleet's dev port), so locally the token and the
enablement are all it needs:

```sh
cd /path/to/Chemclaw3
export CHEMCLAW_PROPS_TOKEN=dev-token     # the same variable the server verifies
export CHEMCLAW_CONNECTORS_ENABLED="<the connectors you want>:props"   # pathsep list; replaces the default set
# export CHEMCLAW_CONNECTOR_URLS='{"props":"http://127.0.0.1:8850/mcp"}'   # only for a non-default address

make connector-validate                   # the manifests resolve and are classified
uvicorn chemclaw.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

For `pyexec`, or to make this repository's manifests win, prepend `manifests/` — keeping
Chemclaw3's own directory on the path, or every shipped bundle disappears:

```sh
CHEMCLAW_OWN=$(uv run python -c "import chemclaw.connectors, pathlib; print(pathlib.Path(chemclaw.connectors.__file__).parent)")
export CHEMCLAW_CONNECTORS_DIR="/path/to/Chemclaw3-mcp/manifests:$CHEMCLAW_OWN"
export CHEMCLAW_PYEXEC_TOKEN=dev-token
```

Then ask the agent something only this server can answer — *"what is the flash point of 2-MeTHF,
and what could replace dichloromethane in a plant?"* — and confirm the tool was **called**, not
recalled. The `source` field in the answer is the tell: it names the vendored dataset and its
licence.

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

Chemclaw3 keeps its `calc` bundle and its whole tool surface. What moves is the *computation*
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

The step-by-step — apply, Secret, image, the Chemclaw3 values for each server — is
[`operations.md`](operations.md). What follows is why it is shaped that way.

On Chemclaw3's side, one value says the server is hosted elsewhere and gives the address:

```yaml
connectors:
  props:
    enabled: true
    server: true
    url: http://chemclaw-mcp-props:8850/mcp
```

Presence of `url` is the flag — there is no separate `external: true`, deliberately, because a
boolean and an address are two declarations of one fact that can disagree. That bundle then gets no
app Deployment and no Service from Chemclaw3's chart. `server: true` stays: it mirrors whether the
manifest declares an `endpoint:`, and the chart ignores a `url` beside `server: false`.

**A bare short name, in the same namespace.** Every `servers/*/deploy/networkpolicy.yaml` here
admits its caller with a bare `podSelector`, and a peer with a `podSelector` and no
`namespaceSelector` selects pods **in the policy's own namespace** — so a namespace-qualified address
resolves in DNS and is then dropped by the server's own ingress rule
(`D-2026-09-07-a-seam-that-stops-at-the-chart-is-not-a-seam` in Chemclaw3 records the same failure,
and is why its chart ships short names). Running the fleet in its own namespace is a change **here**
first — a `namespaceSelector` peer per policy — and then an address there; doing it in the other
order gets a connector that is configured, resolves, and times out.

On this side, each server ships:

- a rootless image built from `servers/<name>/Containerfile`, **built with
  `--build-arg CHEMCLAW_REVISION=$(git rev-parse HEAD)`** — see below;
- `servers/<name>/deploy/` — Deployment, Service, NetworkPolicy (default-deny egress, ingress from
  the Chemclaw3 pods and the Prometheus scraper only), HPA, PDB and ServiceMonitor;
- `MCP_ALLOWED_HOSTS` in `deploy/deployment.yaml`, set to the server's own Service `name:port`
  (`chemclaw-mcp-props:8850`). `/mcp` sits behind upstream's DNS-rebinding guard, which admits a
  loopback `Host` only, so without it every call through the Service is a `421 Misdirected Request`
  — while `/healthz`, outside the MCP app, stays green and nothing else notices. **An address
  Chemclaw3 dials by any other name needs that name added here too**: an ingress host, a renamed
  Service, a port-forward to a non-loopback name. Entries are comma-separated `host:port` or
  `host:*`; a URL, a missing port or a wildcard host is refused at startup, naming the entry
  (`D-2026-10-02-the-rebinding-guard-stays-on-and-is-told-the-service-name`).

It also wires the bearer: the variable the manifest names as `auth.token_env`
(`CHEMCLAW_PROPS_TOKEN` for `props`), from a `secretKeyRef` into `chemclaw-secrets` with that
variable as the key — the Secret Chemclaw3's chart reads (`secrets.name`), in the same namespace, so
Chemclaw3 reads it to send and the server reads it to verify, one value. What the operator supplies
is the key in that Secret (`operations.md` §2); without it the pod does not start, which is louder
than a server that starts and refuses every `/mcp` call with 401 while `/healthz` stays green.

### The revision is a build argument, and forgetting it is silent

Chemclaw3's audit row records the *orchestrator's* commit. Since the chemistry moved here, the
process that computed a number ships on this repository's cadence instead, and that column no longer
names it. So `MCP_SERVER_REVISION` rides the `initialize()` handshake: `connector_app` stamps it onto
`serverInfo.version`, which every client already reads when it opens a session, and onto `/healthz`
for a probe that has no session. No extra endpoint, no extra round trip, no field on every result.

An image built without the build argument answers `"unknown"` rather than failing.
`tests/test_fleet.py` asserts the `ARG`/`ENV` pair in each Containerfile, and the Jenkins pipeline's
verify stage refuses an image whose `/healthz` does not report the revision it was built from — so a
hand-run build that drops the `--build-arg` is the remaining way to get `"unknown"`, and
`curl .../healthz` shows it.

### Which manifests reach the Chemclaw3 pod

For the eight connectors Chemclaw3 declares itself, none: its image already carries them. For
`pyexec`, mount `manifests/pyexec/connector.yaml` as a ConfigMap through the chart's
`extraConnectors.bundles` (prepended to `CHEMCLAW_CONNECTORS_DIR`). Mounting more of `manifests/`
is allowed and makes this repository's copy win each name it carries, with the `default_enabled`
consequence described [above](#what-chemclaw3-already-declares). `manifests-internal/` is never a
path to add: it is the directory whose contents must not be discovered.

**`calc` and `rxnlabel` deploy like the rest and are registered like none of them.** Same images,
same NetworkPolicies, same bearer Secrets (`CHEMCLAW_CALC_TOKEN`, `CHEMCLAW_RXNLABEL_TOKEN`) — but
Chemclaw3 addresses the first from inside `cached_compute` (`CHEMCLAW_CALC_SERVER_URL`) and the
second from a background drain (`CHEMCLAW_RXNLABEL_SERVER_URL`), and their manifests must never
reach `CHEMCLAW_CONNECTORS_DIR`.

## Failure modes worth knowing

The operator-facing table — 421, 401, readiness 503s, session and admission refusals, egress
refusals — is in [`operations.md`](operations.md#5-troubleshoot). These are the ones specific to the
seam:

| Symptom | Cause |
| --- | --- |
| The agent answers a solvent question from memory, with no `source` | The connector is unreachable and degraded silently. Check `/readyz`. |
| Every MCP call returns 401 | The token env var is unset or differs between the two pods. It fails closed by design. |
| The server accepts connections then hangs on the first call | The MCP session manager is not running — the mount-does-not-run-a-lifespan trap. `connector_app` handles it; a hand-rolled transport does not. |
| `props` (or another opt-in connector) is bound on every turn although nothing enabled it | This repository's `manifests/` is on `CHEMCLAW_CONNECTORS_DIR` and its copy won the name; of the opt-in manifests only `pyexec`'s carries `default_enabled: false` here. Mount only the bundles you mean to bind, or name the set in `CHEMCLAW_CONNECTORS_ENABLED`. |
| Startup error naming a connector | `CHEMCLAW_CONNECTORS_ENABLED` lists a name no bundle provides. That is deliberate: a typo must not silently remove a capability. For `pyexec`, mount its manifest. |
| `calculator_trust`, `find_calculations` or a durable calc job has vanished from the surface | A `calc` manifest from this fleet reached `CHEMCLAW_CONNECTORS_DIR` and its partial port won the name collision. It cannot come from `manifests/` — check for a hand-copied `connector.yaml`, or a path pointing into `manifests-internal/`. |
| Chemclaw3 refuses to start with `invalid manifest: ... mount ... Extra inputs are not permitted` | `manifests-internal/` is on `CHEMCLAW_CONNECTORS_DIR`. That is the guard working: those servers are addressed by configuration (`CHEMCLAW_CALC_SERVER_URL`, `CHEMCLAW_RXNLABEL_SERVER_URL`), never discovered. Remove the path. |
| Every calculation recomputes; the cache never hits | The key was derived locally instead of read from `calculation_key`, or a `CALCULATION_EPOCH` was bumped on either side (which invalidates every row deliberately — the two compose). The parts `store.get` needs come back from that tool ready to use; nothing on the Chemclaw3 side should be assembling one. |
| `calculator_trust("pka")` says `UNCALIBRATED` with n=0 on a calculator that has residuals | A `calc_version` was re-derived rather than read off the result. The ledger matches it exactly and does not pool versions, so a locally-built string — which comes out well-formed, because `binary_version()` answers `"absent"` rather than raising — matches nothing. |
| An xTB call takes minutes | Nothing is cached *on this server*. That is what `calculation_key` plus Chemclaw3's own store is for; a cold `optimize_geometry` on a drug-sized molecule is tens of seconds to minutes and a `compute_hessian` is 6N single points, and the manifest allows 900 s. |
| A `calc` call is abandoned by Chemclaw3 while this pod keeps computing | Chemclaw3's `CHEMCLAW_CALC_*_TIMEOUT_SECONDS` was lowered, or this server's budget raised, past the 120 s margin between them. See `servers/calc/README.md`, "Cost". |
| `connector-validate` fails on `auth` | A non-loopback URL with `mode: none`. Declare bearer. |
