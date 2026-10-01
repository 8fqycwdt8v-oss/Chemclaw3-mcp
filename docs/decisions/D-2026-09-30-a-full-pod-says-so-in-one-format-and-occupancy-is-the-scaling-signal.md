# D-2026-09-30-a-full-pod-says-so-in-one-format-and-occupancy-is-the-scaling-signal — A full pod says so in one fleet-wide format, and admission occupancy is the scaling signal

**Status:** accepted · **Date:** 2026-09-30 · **Builds on:**
`D-2026-09-12-one-tool-call-is-not-one-thread` (a slot is a core, and the gate counts what the pod
spends). **Serves:** Chemclaw3's queued-compute change, which puts every heavy tool call behind a
Temporal queue and retries a full pod at seconds instead of refusing it to the chemist.

## What was found

- **Only `calc` could tell a caller its pod was full.** It leads its refusal with
  `[calc-at-capacity]`, the one channel MCP leaves a refused call (a text block and `isError`, no
  code, no structured payload). `chem`, `kinetics`, `pyexec`, `rxnlabel` and `rxnpredict` refused
  with a plain `ValueError`, so to Chemclaw3 a full `rxnpredict` pod was indistinguishable from a
  malformed SMILES and was never retried.
- **No scrape could say how full a pod was.** The gate held its count in a private field;
  `/metrics` had the session pair and nothing about slots. Every HPA therefore scaled on CPU, which
  on this fleet under-reads a saturated pod: a `calc` pod holding all four slots on in-process xTB
  draws 1.37 cores (`servers/calc/deploy/hpa.yaml`), so a full pod reads as a third busy.

## What was decided

- **One marker format, owned by the kit**: `mcp_server_kit.limits.at_capacity_marker(server)` is
  `[<server>-at-capacity]`, and `Admission.refuse(sentence)` builds the `AtCapacityError` (a
  `ValueError`, so `connector_app` still passes it verbatim) with the marker at the head. The
  sentence stays per server, as before. `calc`'s token is this format's value for `"calc"`, so every
  caller already matching it is unchanged.
- **A gate names its server** (`Admission.server`, or the `server=` keyword), and an unnamed gate is
  refused at construction: it would publish under an empty label and mint a marker nobody matches.
- **The gate publishes its occupancy**: `chemclaw_mcp_admission_in_flight`,
  `chemclaw_mcp_admission_ceiling` and `chemclaw_mcp_admission_refused_total`, labelled by server
  only — a slot count names no caller, so the unauthenticated-endpoint rule holds.
- **Occupancy-based autoscaling ships as an opt-in alternative, not a replacement**:
  `servers/<gated>/deploy/keda/scaledobject.yaml`, a KEDA ScaledObject (OpenShift's Custom Metrics
  Autoscaler) scaling on `sum(in_flight) / max(ceiling)` at 0.7 per replica, with the CPU target as
  its second trigger. Same floor, ceiling and behaviour as the CPU HPA. It lives in a subdirectory
  because `oc apply -f deploy/` does not recurse and two autoscalers on one Deployment fight.

## Alternatives weighed

- **Make KEDA the default and delete the CPU HPA.** Declined: whether the operator is installed on
  the target cluster is not known, and a ScaledObject on a cluster without KEDA is a resource the API
  server rejects — the capability would ship with no autoscaler at all.
- **Queue in the gate instead** (an `asyncio.Semaphore`). Still declined, for
  `mcp_server_kit.limits.Admission`'s reason: a queue per pod behind a round-robin Service holds a
  call on a busy pod while its neighbour idles, and outlives the caller's timeout. The queue belongs
  where it is global and durable — Chemclaw3's Temporal task queue — and this change is what lets
  that side tell a full pod from a bad input for every server.
- **Scale on the refusal rate.** Lagging by construction (it rises only once callers are turned
  away) and zero when the caller queues upstream, which is exactly the state the Chemclaw3 change
  creates. Occupancy is leading and stays meaningful behind a queue.

**Revisit when:** the Custom Metrics Autoscaler is confirmed on the target cluster — then the
ScaledObject can become the shipped default and `hpa.yaml` the fallback — or when a server's
occupancy stops tracking its cost (a gate that counts calls rather than cores).

## What keeps it true

- `packages/mcp_server_kit/tests/test_limits.py::test_an_unnamed_gate_is_refused_at_construction`
- `packages/mcp_server_kit/tests/test_limits.py::test_a_full_pod_refusal_leads_with_the_fleet_marker`
- `packages/mcp_server_kit/tests/test_limits.py::test_the_admission_gauges_follow_the_gate`
- `tests/test_fleet.py::test_a_full_pod_says_so_in_the_fleet_s_one_format`
- `tests/test_deploy_shape.py::test_the_keda_alternative_is_the_hpa_with_a_better_signal`
- `tests/test_deploy_shape.py::test_the_keda_alternative_is_not_applied_with_the_rest_of_deploy`
