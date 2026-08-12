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
| `connector-validate` fails on `auth` | A non-loopback URL with `mode: none`. Declare bearer. |
