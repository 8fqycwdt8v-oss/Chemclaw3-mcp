# Operating the fleet: build, deploy, wire, verify, troubleshoot

The runbook for whoever runs these servers. It covers one server end to end, and every server
follows the same steps. What changes from one server to the next is its name, its port, its token
variable and its own knobs: the table in [§3](#3-wire-it-into-chemclaw3) has the first three, and
each `servers/<name>/README.md` has the knobs under **Operating it**.

Related documents:
- [`delivery.md`](delivery.md): the Jenkins pipeline that builds, verifies and publishes images.
- [`integration.md`](integration.md): the Chemclaw3 side in depth, including the `calc` cache seam.
- [`autoscaling.md`](autoscaling.md): the CPU HPA and the opt-in KEDA swap.

## 1. Build an image

Every server has a `servers/<name>/Containerfile`. The build context is the **repository root**,
because the image copies `uv.lock`, the shared kit and the server:

```sh
podman build -f servers/props/Containerfile \
  --build-arg CHEMCLAW_REVISION=$(git rev-parse HEAD) \
  -t chemclaw-mcp-props:$(git rev-parse --short=12 HEAD) .
```

- **The image installs exactly what `uv.lock` hashed.** The build stage runs
  `uv export --frozen --package chemclaw-mcp-<name>` and installs the result with
  `pip --require-hashes`. If the lockfile is stale, the build fails. Run `uv lock` and commit the
  result; do not switch to a re-resolving install.
- **`--build-arg CHEMCLAW_REVISION` is the only way the revision gets in.** Leave it out and the
  image still builds, but `/healthz` and the MCP `initialize` handshake report `"revision":"unknown"`.
- The runtime stage runs as the numeric UID 1001 in group 0 (any UID a platform assigns works the
  same, §2), sets `HOME=/tmp` and `MCP_EGRESS_GUARD=on`, `EXPOSE`s the server's port, and starts
  `uvicorn chemclaw_mcp_<name>.app:app --host 0.0.0.0 --port <port>`.
- **Heavy images**: `calc` installs `xtb` and `crest` from conda-forge in a separate stage and pins
  `CHEMCLAW_XTB_ENGINE=tblite` (see `servers/calc/README.md`). `rxnpredict` and `rxnlabel` add the
  CPU torch index and bake model weights into the image at build time, then set `HF_HUB_OFFLINE=1`.
  These builds need network access. The running image does not.

In CI, the `Jenkinsfile` runs the same build for every server, verifies each running image, and
publishes by digest. A run with `DRY_RUN=false` and a registry set is refused unless
`RUN_GATE=true`. See [`delivery.md`](delivery.md).

To check an image locally:

```sh
podman run --rm -d -p 127.0.0.1:8850:8850 -e CHEMCLAW_PROPS_TOKEN=dev-token chemclaw-mcp-props:<tag>
curl -s localhost:8850/healthz        # "revision" must be the commit you built
```

## 2. Deploy a server (OpenShift / Kubernetes)

### What ships, per server

`servers/<name>/deploy/` holds the complete workload, and `tests/test_deploy_shape.py` holds every
server's copy to one shape:

| File | What it is |
| --- | --- |
| `deployment.yaml` | `chemclaw-mcp-<name>`: 2 replicas, `runAsNonRoot` with **no pinned UID/GID** (the platform assigns one), the bearer from `chemclaw-secrets` under the manifest's `auth.token_env`, the placeholder image `chemclaw3/chemclaw-mcp-<name>:latest`, read-only root filesystem (except `calc` and `pyexec`, which write at runtime), all capabilities dropped, a size-limited `/tmp` `emptyDir`, `MCP_ALLOWED_HOSTS` set to its own Service, `readinessProbe` on `/healthz` and `livenessProbe` on `/livez`. |
| `service.yaml` | `chemclaw-mcp-<name>`, port `http` = the server's port. |
| `networkpolicy.yaml` | Denies all egress. Allows ingress on the server's port from pods labelled `app.kubernetes.io/name: chemclaw` **in the same namespace**, and from the `monitoring` / `openshift-user-workload-monitoring` namespaces. |
| `hpa.yaml` | CPU HPA, `minReplicas: 2`. |
| `pdb.yaml` | `maxUnavailable: 1`. |
| `servicemonitor.yaml` | Prometheus Operator scrape of `/metrics` (needs the `monitoring.coreos.com` CRDs). |
| `keda/scaledobject.yaml` | Optional, on servers with an admission gate. See [`autoscaling.md`](autoscaling.md). |

There is no chart. A release changes an image with `oc set image`. Chemclaw3's
`deploy/jenkins/targets/openshift.sh` does this for the fleet before the core release.

### Prerequisites

- **The same namespace as the Chemclaw3 pods.** The NetworkPolicy admits callers with a
  `podSelector` that has no `namespaceSelector`, which only matches pods in the policy's own
  namespace. Chemclaw3's chart labels every pod `app.kubernetes.io/name: chemclaw`. If you run the
  fleet in a separate namespace, first add a `namespaceSelector` peer to every policy, then use the
  qualified address. In the other order the connector resolves and then times out.
- The `ServiceMonitor` CRD, for the scrape. Without it, skip that one file.
- **The bearer in `chemclaw-secrets`.** Every Deployment reads the variable its manifest names as
  `auth.token_env` from the Secret `chemclaw-secrets`, key = that variable name — the Secret
  Chemclaw3's chart reads (`secrets.name`), so both halves hold one value. The reference is not
  `optional`: if the key is missing the pod stays in `CreateContainerConfigError` rather than
  starting and refusing every call. If your release renamed `secrets.name`, patch the
  `secretKeyRef.name` in your overlay.
- **No SCC grant on OpenShift.** No Deployment pins `runAsUser`, `runAsGroup` or `fsGroup`, so the
  default `restricted-v2` SCC assigns a UID from the namespace's range (GID 0) and admits the pod.
  The images are built for that: a numeric `USER` in group 0, `HOME=/tmp`, and nothing written
  outside the `/tmp` `emptyDir`. `/healthz` is the check that an assigned UID works — on `pyexec`
  it forks a real sandbox run.

### Steps

```sh
NAME=props                                      # the server
TOKEN_ENV=CHEMCLAW_PROPS_TOKEN                  # its manifest's auth.token_env (table in §3)

# 1. The bearer, in the Secret Chemclaw3 reads (skip if the key is already there; §3 needs the
#    same value on the Chemclaw3 side). Key = variable name, which the Deployment references.
oc patch secret chemclaw-secrets --type merge \
  -p "{\"stringData\":{\"$TOKEN_ENV\":\"$(openssl rand -hex 32)\"}}"

# 2. The workload. `-f <dir>` does not recurse, so keda/ is not applied.
oc apply -f servers/$NAME/deploy/

# 3. Pin the image you built. The manifest's `chemclaw3/chemclaw-mcp-<name>:latest` is a
#    placeholder nothing publishes; never run it unrewritten.
oc set image deployment/chemclaw-mcp-$NAME server=<registry>/chemclaw-mcp-$NAME@sha256:<digest>
```

`kubectl` takes the same verbs. Two cautions, and the overlay that removes the second:

- **Step 1 is required.** Without the key the pod never starts (`CreateContainerConfigError`
  naming it). With a key whose value differs from Chemclaw3's, `/mcp` answers 401 while `/healthz`
  stays ready — the fail-closed design, which in a cluster looks like a working server.
- **Re-applying `deployment.yaml` resets `image:` to the placeholder**, because `image:` is in the
  applied manifest. Run step 3 again after any `oc apply`, or keep the digest in a kustomize
  overlay.

**The durable form is a kustomize overlay** kept in your deployment repository, which pins the
image by digest — so `oc apply -k` is repeatable and nothing depends on remembering step 3. The
bearer and the OpenShift-compatible security context are already in the shipped Deployment, so the
overlay needs nothing else:

```yaml
# overlays/props/kustomization.yaml
resources:
  - <path-to-Chemclaw3-mcp>/servers/props/deploy/deployment.yaml
  - <path-to-Chemclaw3-mcp>/servers/props/deploy/service.yaml
  - <path-to-Chemclaw3-mcp>/servers/props/deploy/networkpolicy.yaml
  - <path-to-Chemclaw3-mcp>/servers/props/deploy/hpa.yaml
  - <path-to-Chemclaw3-mcp>/servers/props/deploy/pdb.yaml
  - <path-to-Chemclaw3-mcp>/servers/props/deploy/servicemonitor.yaml
images:
  - name: chemclaw3/chemclaw-mcp-props     # matches the placeholder exactly
    newName: <registry>/chemclaw-mcp-props
    digest: sha256:<digest>
```

`tests/test_deploy_shape.py` holds every Deployment to that one placeholder string, so an
`images:` entry written this way cannot silently miss.

### Environment: what every server reads

Defaults are what a pod runs with when nothing is set. Every bound in this table and every
server-specific bound is reported under `bounds` on `/healthz` at the value the process actually
resolved. Use `/healthz`, not the manifest, to see what an overlay or a `set env` changed.

| Variable | Default | What it does |
| --- | --- | --- |
| `CHEMCLAW_<NAME>_TOKEN` | **unset → every `/mcp` call is 401** | The bearer the server verifies. Its exact name is the manifest's `auth.token_env`; the shipped Deployment sets it from `chemclaw-secrets`. |
| `MCP_ALLOWED_HOSTS` | loopback only | Extra `Host` values `/mcp` accepts, comma-separated `host:port` or `host:*`. Each shipped Deployment sets its own Service (`chemclaw-mcp-props:8850`). Add any other name callers use. A URL, a missing port or a wildcard host stops startup with a message naming the entry. |
| `MCP_MAX_SESSIONS` | `1024` | Live MCP sessions per pod. A handshake past this gets **503** with `Retry-After: 10`. `0` = unbounded. |
| `MCP_SESSION_IDLE_TIMEOUT_SECONDS` | `1800` | How long an unused session lives before it is reaped. Time inside a running tool call does not count as idle. `0` = never reaped. |
| `MCP_SESSION_UNUSED_TIMEOUT_SECONDS` | `60` (capped at the idle timeout) | How long a newly opened session may wait for its first use. |
| `MCP_THREAD_POOL_SIZE` | ⌈cgroup CPU limit⌉ + headroom | Worker threads that tool bodies run in. |
| `MCP_THREAD_POOL_HEADROOM` | `4` | Threads added on top of the CPU limit when the size is derived. |
| `MCP_MAX_SMILES_CHARS` | `4000` | Longest SMILES accepted, checked before parsing (`chem`, `safety`, `rxnlabel`, `rxnpredict`). |
| `MCP_MAX_MOLECULE_ATOMS` | `2000` | Most atoms accepted, checked before canonicalising. Capped at what the process stack survives, so a value above that stops startup. |
| `MCP_MAX_ECHO_CHARS` | `120` | How much of a refused input the error message quotes. |
| `MCP_LOG_LEVEL` / `MCP_LOG_FORMAT` / `MCP_LOG_JSON` | `INFO` / timestamp-level-logger-correlation / off | Process logging. `MCP_LOG_JSON=1` gives one JSON object per line. |
| `MCP_TRACING_ENABLED` | off | Continue Chemclaw3's `traceparent` as a span per tool call. Nothing is exported unless the deployment installs an OpenTelemetry SDK and destination. |
| `MCP_EGRESS_GUARD` | `on` (set in every image) | The runtime no-egress guard. `off`/`0`/`false`/`no` disarms it, and `chemclaw_mcp_egress_guard_armed` shows that on a scrape. Leave it on. |
| `MCP_EGRESS_ALLOW` | empty | Hosts the guard lets through. Empty in every shipped deployment, and only for scripts outside a serving image. |
| `MCP_SERVER_REVISION` | set from the build argument | Reported on `/healthz`, `/livez` and the handshake. Do not set it by hand. |

The per-request body cap is 1 MB (`413` above it). It is a code constant, not a variable.

## 3. Wire it into Chemclaw3

Each server is reached in one of three ways. Settings named `CHEMCLAW_*` belong to Chemclaw3 and are
read from `chemclaw.core.config`. The Helm keys are in that repository's
`deploy/helm/chemclaw/values.yaml`.

| Server | Port | In-cluster URL | Token variable | How Chemclaw3 reaches it |
| --- | --- | --- | --- | --- |
| `chem` | 8858 | `http://chemclaw-mcp-chem:8858/mcp` | `CHEMCLAW_CHEM_TOKEN` | connector, declared by Chemclaw3, on by default |
| `safety` | 8859 | `http://chemclaw-mcp-safety:8859/mcp` | `CHEMCLAW_SAFETY_TOKEN` | connector, declared by Chemclaw3, on by default |
| `rxnpredict` | 8857 | `http://chemclaw-mcp-rxnpredict:8857/mcp` | `CHEMCLAW_RXNPREDICT_TOKEN` | connector, declared by Chemclaw3, on by default |
| `props` | 8850 | `http://chemclaw-mcp-props:8850/mcp` | `CHEMCLAW_PROPS_TOKEN` | connector, declared by Chemclaw3, **off** until enabled |
| `thermalsafety` | 8851 | `http://chemclaw-mcp-thermalsafety:8851/mcp` | `CHEMCLAW_THERMALSAFETY_TOKEN` | connector, declared by Chemclaw3, **off** until enabled |
| `kinetics` | 8852 | `http://chemclaw-mcp-kinetics:8852/mcp` | `CHEMCLAW_KINETICS_TOKEN` | connector, declared by Chemclaw3, **off** until enabled |
| `unitops` | 8853 | `http://chemclaw-mcp-unitops:8853/mcp` | `CHEMCLAW_UNITOPS_TOKEN` | connector, declared by Chemclaw3, **off** until enabled |
| `suitability` | 8892 | `http://chemclaw-mcp-suitability:8892/mcp` | `CHEMCLAW_SUITABILITY_TOKEN` | connector, declared by Chemclaw3, **off** until enabled |
| `pyexec` | 8899 | `http://chemclaw-mcp-pyexec:8899/mcp` | `CHEMCLAW_PYEXEC_TOKEN` | connector, **not** declared by Chemclaw3: mount this repository's manifest |
| `calc` | 8860 | `http://chemclaw-mcp-calc:8860/mcp` | `CHEMCLAW_CALC_TOKEN` | backend: `CHEMCLAW_CALC_SERVER_URL` |
| `rxnlabel` | 8865 | `http://chemclaw-mcp-rxnlabel:8865/mcp` | `CHEMCLAW_RXNLABEL_TOKEN` | backend: `CHEMCLAW_RXNLABEL_SERVER_URL` |

`MODULES.md` is the port registry. The ports above come from the manifests in this repository.

### A connector Chemclaw3 already declares

Chemclaw3's image includes a `connector.yaml` for `chem`, `safety`, `rxnpredict`, `props`,
`thermalsafety`, `kinetics`, `unitops` and `suitability`. Each describes the server here, with the
same name, tools and token variable. You do not need to mount this repository's `manifests/` for
them. On the Chemclaw3 release:

```yaml
connectors:
  props: {enabled: true, server: true, url: http://chemclaw-mcp-props:8850/mcp}   # → CHEMCLAW_CONNECTOR_URLS, CHEMCLAW_CONNECTORS_ENABLED
networkPolicy:
  egressPorts: {props: 8850}            # already in the shipped values for every server above
  egressDestinations:                   # a peer that selects the fleet's pods
    - podSelector: {matchLabels: {app.kubernetes.io/part-of: chemclaw3}}
secrets:
  optionalKeys: {propsToken: CHEMCLAW_PROPS_TOKEN}   # chem/safety/calc/rxnpredict/rxnlabel already have a slot
```

The secret named by `secrets.name` (default `chemclaw-secrets`) must hold `CHEMCLAW_PROPS_TOKEN`.
It is the same Secret the server's Deployment reads (§2), so the two sides hold one value.

The egress peer above matches the pods every `servers/*/deploy/deployment.yaml` labels
`app.kubernetes.io/part-of: chemclaw3` in the same namespace; Chemclaw3's own chart does not use that
label. The chart takes either `egressDestinations` or `allowAnyDestination: true`, never both — a
release already on the latter needs no entry.

`props`, `thermalsafety`, `kinetics`, `unitops` and `suitability` declare `default_enabled: false`
on the Chemclaw3 side (their copies here do not yet; see below). `pyexec`, which only this
repository declares, carries `default_enabled: false` here. With an empty `CHEMCLAW_CONNECTORS_ENABLED` they are not bound. Naming one
with `connectors.<name>.enabled: true` binds it. Each bound connector adds its tool schemas to the
prompt of every model call, so enable only what a site uses.

`chem`, `rxnpredict`, `kinetics` and `pyexec` list `queued:` tools. Chemclaw3 runs an interactive
Temporal worker for them, sized by `connectors.<name>.interactive`. Size it to the server's
`replicas x admission ceiling` (see each README).

### `pyexec`: mount this repository's manifest

Chemclaw3 does not declare `pyexec`, so the manifest has to come from here. Make a ConfigMap
containing `manifests/pyexec/connector.yaml` and list it in `extraConnectors.bundles`:

```sh
oc create configmap chemclaw-connector-pyexec --from-file=connector.yaml=manifests/pyexec/connector.yaml
```

```yaml
extraConnectors:
  bundles: [{name: pyexec, configMap: chemclaw-connector-pyexec}]
connectors:
  pyexec:
    enabled: true
    server: true
    url: http://chemclaw-mcp-pyexec:8899/mcp
    interactive: {replicas: 2, maxReplicas: 6, maxConcurrentActivities: 2}
networkPolicy:
  egressPorts: {pyexec: 8899}           # not in the shipped values; add it
secrets:
  optionalKeys: {pyexecToken: CHEMCLAW_PYEXEC_TOKEN}
```

**Mount only the bundles you need, and never `manifests-internal/`.** The chart prepends the mount
to `CHEMCLAW_CONNECTORS_DIR`, and the first directory wins a name collision. A manifest from this
repository therefore replaces Chemclaw3's copy of the same connector. Of the opt-in servers only
`pyexec`'s manifest here carries `default_enabled: false`; the other five do not yet (Chemclaw3's
`tests/test_sibling_manifest_agreement.py` records that difference as argued, so the two repositories
have to change it together). A mounted `props` (for example) is therefore bound on every turn when
`CHEMCLAW_CONNECTORS_ENABLED` is empty — a chart release never renders it empty, but any other wiring
should name the set. Bundle skills are still merged from every directory with the same name.

### `calc` and `rxnlabel`: backends, never connectors

Chemclaw3 calls `calc` from inside its calculation cache and `rxnlabel` from a background labelling
drain. Their manifests are in `manifests-internal/`, declare `mount: backend`, and make Chemclaw3
refuse to start if they reach `CHEMCLAW_CONNECTORS_DIR`. Configure them as plain settings:

| Chemclaw3 setting | Default | Value |
| --- | --- | --- |
| `CHEMCLAW_CALC_SERVER_URL` | `http://127.0.0.1:8860/mcp` | `http://chemclaw-mcp-calc:8860/mcp` (the chart's value) |
| `CHEMCLAW_CALC_SERVER_TOKEN_ENV` | `CHEMCLAW_CALC_TOKEN` | the variable name, not the token |
| `CHEMCLAW_CALC_SERVER_TIMEOUT_SECONDS` | `900` | ordinary calculations |
| `CHEMCLAW_CALC_ATOMIC_TIMEOUT_SECONDS` | `3600` | the two binary-only calculations |
| `CHEMCLAW_CALC_SAMPLING_TIMEOUT_SECONDS` | `14400` | CREST searches |
| `CHEMCLAW_RXNLABEL_SERVER_URL` | `http://127.0.0.1:8865/mcp` | `http://chemclaw-mcp-rxnlabel:8865/mcp` (the chart's value) |
| `CHEMCLAW_RXNLABEL_SERVER_TOKEN_ENV` | `CHEMCLAW_RXNLABEL_TOKEN` | the variable name |
| `CHEMCLAW_RXNLABEL_SERVER_TIMEOUT_SECONDS` | `120` | one labelling batch |
| `CHEMCLAW_LABEL_BATCH_SIZE` | `200` | must stay ≤ the server's `CHEMCLAW_RXNLABEL_MAX_BATCH` (500) |

Each server's own timeouts are its caller's timeout minus 120 s (`servers/calc/README.md`). If you
raise one side, raise the other too. The chart's `egressPorts` already lists `calc: 8860` and
`rxnlabel: 8865`, and `secrets.optionalKeys` already has `calcToken` and `rxnlabelToken`. Do not
confuse `CHEMCLAW_CALC_TOKEN` (Chemclaw3 to this `calc` server) with `CHEMCLAW_CALC_MCP_TOKEN`
(Chemclaw3's own `calc` bundle).

For local development without a cluster, see [`integration.md`](integration.md#local-development).

## 4. Verify

Port-forward the Service. Use `localhost` so the `Host` header is accepted without changing
`MCP_ALLOWED_HOSTS`:

```sh
oc port-forward svc/chemclaw-mcp-props 8850:8850 &
curl -s localhost:8850/livez     # {"status":"alive","server":"props","revision":"<sha>"}
curl -s localhost:8850/healthz   # 200: status ok, revision, bounds, datasets ["process-solvents@0.2.0"]
curl -s localhost:8850/metrics | grep '^chemclaw_mcp_ready'
```

`/healthz`, `/livez` and `/metrics` need no token. `/healthz` returns `503` with `status: unready`
and a `reason` when the server cannot serve. It returns `200` with a `degraded` field when the
failure is transient. A server with no corpus reports `datasets: []`, or names its first-party
constants (`thermalsafety`, `kinetics`, `unitops`, `suitability`).

A real tool call over MCP (streamable HTTP):

```sh
T=<the token>; U=localhost:8850/mcp
H=(-H "Authorization: Bearer $T" -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream')
SID=$(curl -s -D - -o /dev/null "${H[@]}" $U -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}' \
      | awk -F': ' 'tolower($1)=="mcp-session-id"{print $2}' | tr -d '\r')
curl -s "${H[@]}" -H "Mcp-Session-Id: $SID" $U -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'
curl -s "${H[@]}" -H "Mcp-Session-Id: $SID" $U -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
curl -s "${H[@]}" -H "Mcp-Session-Id: $SID" $U -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"solvent_properties","arguments":{"name":"2-MeTHF"}}}'
curl -s -X DELETE "${H[@]}" -H "Mcp-Session-Id: $SID" $U     # release the session
```

To test what Chemclaw3 will see, send the same `initialize` from a Chemclaw3 pod to the Service
name: `oc rsh <chemclaw-pod> curl ... http://chemclaw-mcp-props:8850/mcp`. A `421` from there
means `MCP_ALLOWED_HOSTS` is wrong. A timeout means the NetworkPolicy dropped the connection.

On the Chemclaw3 side, check `/readyz` (count of unhealthy connectors) and
`chemclaw_connectors_unhealthy` on its `/metrics` (their names). A connector it cannot reach does
not show an error. Its tools are missing from the turn.

### Metrics worth alerting on

| Series | Meaning |
| --- | --- |
| `chemclaw_mcp_ready{server}` | 1 when the last `/healthz` was ready |
| `chemclaw_mcp_build_info{server,revision}` | which build is running |
| `chemclaw_mcp_tool_calls_total{server,tool,outcome}` / `chemclaw_mcp_tool_duration_seconds` | per-tool rate, errors and latency |
| `chemclaw_mcp_requests_total` / `chemclaw_mcp_unauthenticated_requests_total` | HTTP traffic, and 401s (a missing or mismatched token) |
| `chemclaw_mcp_sessions_live` / `_ceiling` / `_refused_total` | MCP sessions against `MCP_MAX_SESSIONS` |
| `chemclaw_mcp_admission_in_flight` / `_ceiling` / `_refused_total` | busy work slots on servers with an admission gate |
| `chemclaw_mcp_degraded_total{server,component,cause}` | answers given with a component's contribution missing |
| `chemclaw_mcp_egress_refused_total` / `chemclaw_mcp_egress_guard_armed` | outbound attempts blocked, and whether the guard is on |

## 5. Troubleshoot

| Symptom | Cause | Fix |
| --- | --- | --- |
| `/mcp` returns **421** `Invalid Host header`; `/healthz` is 200 | The `Host` header (the name the caller dialled) is not in `MCP_ALLOWED_HOSTS`. Upstream's DNS-rebinding guard accepts loopback by default. | Add the dialled `host:port` to `MCP_ALLOWED_HOSTS`. The shipped Deployment sets only the Service short name. A qualified name, a Route or a renamed Service each need an entry. |
| `/mcp` returns **403** `Invalid Origin header` | A browser-style `Origin` header that does not match an allowed host. | Server-to-server callers do not send `Origin`. If a proxy adds one, strip it, or add the host to `MCP_ALLOWED_HOSTS` (origins follow hosts). |
| Every `/mcp` call is **401** `unauthorized` | The two sides hold different values or different variable names — e.g. the server reads a different Secret than Chemclaw3 (a renamed `secrets.name` not carried into the overlay). | `oc set env deployment/chemclaw-mcp-<name> --list` must show the manifest's `auth.token_env` from `chemclaw-secrets`. Compare with the Chemclaw3 secret. `chemclaw_mcp_unauthenticated_requests_total` counts these. |
| Chemclaw3 raises `MissingConnectorCredential` | The token is unset on the **Chemclaw3** pod. | Add it to Chemclaw3's secret (`secrets.optionalKeys`). |
| Pod stuck in `CreateContainerConfigError` naming `CHEMCLAW_<NAME>_TOKEN` | The key is missing from `chemclaw-secrets` (the reference is deliberately not optional), or the Secret is in another namespace. | Add the key (§2, step 1) in the release's namespace. |
| No pods; ReplicaSet event `unable to validate against any security context constraint` | Something re-added a pinned `runAsUser`/`runAsGroup`/`fsGroup` (an old overlay); the shipped Deployments pin none. | Remove the pin from the overlay; `restricted-v2` assigns the UID. |
| `ErrImagePull` / `ImagePullBackOff` on `chemclaw3/chemclaw-mcp-<name>:latest` | The placeholder image was applied unrewritten. | Pin the published digest (§2, step 3 or the overlay's `images:`). |
| Pod not Ready; `/healthz` **503** with `reason` naming a file and two hashes | A vendored corpus failed its checksum: `… does not match the approved checksum: manifest says <a>, file is <b>`. | The image holds a file different from the one reviewed. Rebuild from a clean checkout. Never edit `dataset.json` in place to match. |
| `/healthz` 503 naming `no dataset manifest` / `no records file` / a missing provenance field | The image is missing a corpus or its `dataset.json`. | Rebuild. The `Containerfile` copies the whole server directory. |
| `/healthz` 503 on `calc`: `CHEMCLAW_XTB_ENGINE selects the xtb binary and this image has none on PATH` | `CHEMCLAW_XTB_ENGINE=xtb` on an image without `xtb`. | Unset it (the image pins `tblite`) or use the shipped image, which includes the binary. |
| `/healthz` 503 on `rxnpredict` / `rxnlabel` naming a predictor or component | A model the image includes failed to load (missing checkpoint, broken install), or an `ENABLED_*_MODELS` allow-list names an unregistered predictor. | See that server's README. An extra that is simply not installed is ready, not a fault. |
| `/healthz` 200 with `"degraded"` | A transient cause (resource exhaustion) during the probe. The pod stays in its Service. | Watch `chemclaw_mcp_degraded_total`. Act if it persists. |
| Pod restarts (`CrashLoopBackOff`) | `/livez` stopped answering (the process is wedged or OOMKilled), or startup failed. | Check `oc logs --previous`. Startup refuses a bad `MCP_ALLOWED_HOSTS` entry and a resource bound below its floor (or `MCP_MAX_MOLECULE_ATOMS` above its stack ceiling), naming the variable. |
| Handshake gets **503** with `Retry-After: 10` and a JSON-RPC error saying the server is holding its session ceiling | `MCP_MAX_SESSIONS` reached. | Usually clients that never close sessions. Check `chemclaw_mcp_sessions_live`. Scale out, or raise the ceiling on a pod with more memory (about 57 kB per session). |
| A tool call returns an error starting `[<server>-at-capacity]` | Every admission slot on that pod is busy (`calc`, `chem`, `kinetics`, `pyexec`, `rxnlabel`, `rxnpredict`). The call was refused, not queued. | Transient: the same call succeeds once a slot frees. For sustained load, scale out (`autoscaling.md`). Raise the per-pod ceiling only together with the pod's CPU limit. |
| A tool error says `outbound connection to '<host>' refused` | A library tried to reach the network. The egress guard (`EgressForbidden`) blocked it. This is a bug, not a configuration problem. | Find the library in the log line at ERROR. Bake the data into the image at build time. Do not set `MCP_EGRESS_ALLOW` on a serving pod. |
| A tool error says `an internal error occurred` with an `error_id` | An unexpected exception. The details are not sent to the caller. | `grep <error_id>` in the pod log for the traceback. |
| `413 request body too large` | A request body over 1 MB. | Send less. For `rxnlabel`, use smaller batches. |
| Connector configured, resolves, times out | The NetworkPolicy dropped the connection: a different namespace, a caller without the label `app.kubernetes.io/name: chemclaw`, or Chemclaw3's own egress policy missing the port or the destination. | Same namespace; check `egressPorts` and `egressDestinations` on the Chemclaw3 side. |
| Chemclaw3 will not start: `invalid manifest: … mount … Extra inputs are not permitted` | `manifests-internal/` (or a copy of `calc`/`rxnlabel`'s manifest) is on `CHEMCLAW_CONNECTORS_DIR`. | Remove it. Those two are reached by `CHEMCLAW_CALC_SERVER_URL` / `CHEMCLAW_RXNLABEL_SERVER_URL`. |
| Chemclaw3 will not start: `connectors_enabled names unknown connector(s) ['pyexec']` | Enabled without a manifest. | Mount `manifests/pyexec` (§3). |
| `/healthz` says `"revision":"unknown"` | The image was built without `--build-arg CHEMCLAW_REVISION`. | Rebuild with it. |
| Pod accepts connections, then hangs on the first `/mcp` request | A hand-written transport that does not run the MCP session manager. | Use `mcp_server_kit.connector_app`. Every server here does. |

The server logs every refused unauthenticated request at WARNING. The default log line includes the
caller's correlation and session ids (`[correlation/session]`), so a Chemclaw3 turn can be matched
to the server lines it caused.
