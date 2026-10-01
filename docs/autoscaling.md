# Autoscaling

Every server ships a CPU `HorizontalPodAutoscaler` (`servers/<name>/deploy/hpa.yaml`) and a floor of
two replicas. That works on any cluster, and it is the default.

## The better signal, where the cluster has it

Every server with an admission gate — `calc`, `chem`, `kinetics`, `pyexec`, `rxnlabel`,
`rxnpredict` — publishes how full it is:

| Series | Meaning |
| --- | --- |
| `chemclaw_mcp_admission_in_flight{server}` | slots held right now (a slot is a core) |
| `chemclaw_mcp_admission_ceiling{server}` | the configured ceiling per pod |
| `chemclaw_mcp_admission_refused_total{server}` | calls turned away because every slot was held |

`sum(in_flight) / max(ceiling)` is "how many pods' worth of slots are busy", and it is a better
scaling signal than CPU here: a full `calc` pod on in-process xTB draws 1.37 of its 4 cores, so CPU
reads it as a third busy. Behind Chemclaw3's queued compute it is also the only signal that rises
before anyone is refused, because the backlog waits in Temporal rather than at the pod.

Each gated server ships `deploy/keda/scaledobject.yaml`: a KEDA ScaledObject on that quotient
(target 0.7 per replica) with the CPU target kept as its second trigger, and the same floor, ceiling
and scale-down behaviour as the CPU HPA. KEDA is OpenShift's **Custom Metrics Autoscaler** operator.

### Adopting it for one server

1. Install the Custom Metrics Autoscaler operator and create a `KedaController`.
2. Create the Secret `chemclaw-mcp-<name>-prometheus` with `token` (a ServiceAccount token that may
   read the namespace's metrics through the Thanos querier) and `ca.crt` (the service CA).
3. Edit `namespace:` in the ScaledObject to the namespace the server runs in.
4. Swap the autoscalers — two on one Deployment fight, so this is deliberate:

   ```sh
   oc delete hpa chemclaw-mcp-<name>
   oc apply -f servers/<name>/deploy/keda/
   ```

Revert with `oc delete -f servers/<name>/deploy/keda/ && oc apply -f servers/<name>/deploy/hpa.yaml`.

## Sizing for interactive use

Autoscaling handles sustained load; it cannot answer a burst in real time, because a new pod takes a
minute or more to schedule and pass readiness. So a deployment that wants near-real-time answers for
many concurrent chemists sets the **floor** (`replicas` and `minReplicas`/`minReplicaCount`, kept
equal) to its expected peak of seconds-class work, and lets scaling cover the rest. For `calc`: one
pod is four concurrent single-point-class calculations.

The *why* is `docs/decisions/D-2026-09-30-a-full-pod-says-so-in-one-format-and-occupancy-is-the-scaling-signal.md`.
