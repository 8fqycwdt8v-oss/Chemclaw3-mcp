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
| `servers.<name>.resources`, `.replicas`, `.maxReplicas` | requests/limits and the autoscaler's floor (`minReplicas`) and ceiling; the Deployment itself renders no `replicas` while an autoscaler owns the count |
| `servers.<name>.keda` | the server has an admission gate; `autoscaling.mode=keda` swaps its HPA for a ScaledObject; `autoscaling.mode=none` renders neither and the Deployment carries a fixed `replicas` |
| `secretName` | the Secret holding every bearer, keyed by `CHEMCLAW_<NAME>_TOKEN` |
| `networkPolicy.callerLabels`, `.scraperNamespaces` | the one caller and the Prometheus namespaces each server admits |

Object names are fixed (`chemclaw-mcp-<name>`) whatever the release is called, because Chemclaw3
dials them. The named templates `chemclaw-fleet.service` and `chemclaw-fleet.tokenEnv` return the
Service name and the token variable for a server name, so a parent chart derives the in-cluster URL
(`http://<service>:<port>/mcp`) and the secret key from the same `servers.<name>.port` instead of
declaring them again.

## What a values file can and cannot do

A values file cannot add a field to a pod. Two layers hold that, and either alone is enough for
the cases below:

- `values.schema.json` closes every object (a typo such as `serviceMonitor.enable` is refused, not
  ignored) and gives every string that reaches a manifest a pattern: sizes, quantities, durations,
  DNS names, image registry, prefix, tag and digest, the KEDA address. It also bounds ports to the
  fleet's block. It does **not** judge a tag: `latest` and an empty tag pass the schema's character
  set and are refused by the template (`chemclaw-fleet.image` in `templates/_helpers.tpl`).
- Every template quotes (or `toYaml`s, or `int`s) each value it interpolates, so with the schema
  removed a value carrying a newline is still one string. `tests/test_deploy_shape.py` renders
  newline-bearing values into each field with and without the schema and requires the first to be
  refused and the second to add no key to any object.

Cross-field rules the schema cannot state fail the render by name: two servers on one port, a
`maxReplicas` below `replicas`, a server name that is not lower-case letters and digits.

The chart's `global` key is open and no template reads it: it exists so a parent chart can set
`global.*` without Helm refusing the subchart.

## Servers hosted elsewhere

A server that is not in `servers/` (`retro`, hosted in `chemclaw2_retrosynthesis`) is not in this
chart, and `tests/test_deploy_shape.py::test_the_chart_names_exactly_the_servers_the_tree_has` keeps
it out. Its own repository deploys it; what it owes this fleet is the contract in `CLAUDE.md` (a
classified manifest, a bearer on `/mcp`, no egress, a Service name `/mcp` answers to), including the
`chemclaw-mcp-<name>` Service name and token variable Chemclaw3 will dial.
