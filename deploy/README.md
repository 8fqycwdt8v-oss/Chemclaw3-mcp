# `deploy/`

How the fleet runs in a cluster: one Helm chart, `helm/chemclaw-fleet`, and nothing else. A
server's deployment is its entry in `values.yaml` (`servers.<name>`); the Deployment, Service,
NetworkPolicy, HorizontalPodAutoscaler, PodDisruptionBudget, ServiceMonitor and (opt-in) KEDA
ScaledObject are rendered from it by `templates/`, where each rule's rationale is written once.

```sh
helm template chemclaw-fleet deploy/helm/chemclaw-fleet --namespace chemclaw   # what a cluster receives
make chart-validate                                                             # lint + kubeconform, both autoscaling modes
```

Installing it, and naming the images a release built, is [`docs/operations.md`](../docs/operations.md) §2.
The tests that hold every server's render to one shape are `tests/test_deploy_shape.py` (fleet-wide
invariants) and `tests/test_deploy.py` (per-server ports, ingress, scrape wiring, hardening); both
need `helm` and say so when they skip.

## The interface a parent chart reads

| Value | Meaning |
| --- | --- |
| `servers.<name>.port` | the one declaration of a server's port; Service, container, NetworkPolicy and `MCP_ALLOWED_HOSTS` derive from it |
| `servers.<name>.image.{tag,digest,registry,namePrefix}` | image override, merged over `defaults.image`; `latest` and an empty tag are refused |
| `servers.<name>.resources`, `.replicas`, `.maxReplicas` | requests/limits and the autoscaler's floor and ceiling |
| `servers.<name>.keda` | the server has an admission gate; `autoscaling.mode=keda` swaps its HPA for a ScaledObject |
| `secretName` | the Secret holding every bearer, keyed by `CHEMCLAW_<NAME>_TOKEN` |
| `networkPolicy.callerLabels`, `.scraperNamespaces` | the one caller and the Prometheus namespaces each server admits |

Object names are fixed (`chemclaw-mcp-<name>`) whatever the release is called, because Chemclaw3
dials them. The named templates `chemclaw-fleet.service` and `chemclaw-fleet.tokenEnv` return the
Service name and the token variable for a server name, so a parent chart derives the in-cluster URL
(`http://<service>:<port>/mcp`) and the secret key from the same `servers.<name>.port` instead of
declaring them again. `values.schema.json` rejects an unknown key, a port outside 8850-8899 and a
moving image tag.
