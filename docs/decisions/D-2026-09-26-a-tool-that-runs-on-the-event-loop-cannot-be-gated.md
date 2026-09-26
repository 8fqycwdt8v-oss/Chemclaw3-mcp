# D-2026-09-26-a-tool-that-runs-on-the-event-loop-cannot-be-gated — A tool that runs on the event loop cannot be gated, and the integrator gets a ceiling rather than a new scheme

**Status:** accepted · **Date:** 2026-09-26 · **Builds on:**
`D-2026-09-12-one-tool-call-is-not-one-thread` (whether a tool is gated is its cost, not the
manifest's `read_only`/`state_changing` split). **Supersedes in part:**
`D-2026-09-15-the-dependency-a-catalogue-proposed-was-the-one-tool-it-could-not-carry`'s 836 µs
exemption of `kinetics` from a concurrency ceiling, which held only while the step count was fixed.

## What was found

`servers/kinetics`' `semibatch_accumulation_profile` derives its RK4 step count from the caller's
rate constant and dose time (`reactors._steps_for_stability`) up to `MAX_INTEGRATION_STEPS` =
200,000. The argument that this server owed no concurrency ceiling was made at a fixed 200 steps and
0.83 ms. Measured in the `cc3-gate` image on a loaded host, a 1 h dose against a co-reagent at 60:
24 ms at `k = 0.02`, 58 ms at `k = 0.05`, 585 ms at `k = 0.5`, and 1.8–3.4 s of CPU with 49.6 MB of
traced allocation for the worst legal call (`k = 2.578`, 199,946 steps). The backlog row had it at
~1.2 s on a quieter machine.

The second finding is what a ceiling alone would have missed: the tool was a plain `def`, and
FastMCP 1.x calls a synchronous tool **on the event loop** (`FuncMetadata.call_fn_with_arg_validation`
returns `fn(**arguments)` with no thread hop). One worst-case call stopped every other request on the
process, the kubelet's 3 s `/healthz` probe included, and a ceiling on it could never have tripped,
because two of them could never be in flight at once.

## What was decided

- **The tool is `async` and offloads with `asyncio.to_thread`**, like every heavy tool in the fleet.
- **It is gated on `mcp_server_kit.limits.Admission`** (`engine/admission.py`), refused rather than
  queued, with the slot held until the worker thread finishes (`asyncio.shield`), exactly
  `servers/chem`'s shape. `CHEMCLAW_KINETICS_MAX_CONCURRENT_INTEGRATIONS`, default **3**: the
  integration is pure Python and holds the GIL, so admitted calls run one at a time and N of them
  finish after N × `WORST_INTEGRATION_SECONDS` (2 s); three keeps that under half of the manifest's
  15 s `request_timeout`, and three worst cases fit the pod's 512 Mi.
- **The cheaper integrator the row offered is not taken here.** An implicit or exponentially-fitted
  step would bring the stiff case back down and lift the refusal past `MAX_INTEGRATION_STEPS`, but
  it would not remove the need for a gate — the realistic `k = 0.5` case is already hundreds of
  milliseconds — and it would replace a scheme whose fourth-order convergence was measured with one
  that has not been. It stays queued in `docs/BACKLOG.md` as the thing that would lift the refusal.
- The ceiling was not lowered and `MAX_INTEGRATION_STEPS` was not touched, as the row required.

## What keeps it true

- `servers/kinetics/tests/test_admission.py::test_the_integration_runs_off_the_event_loop`
- `servers/kinetics/tests/test_admission.py::test_a_full_pod_refuses_the_next_integration_before_starting_it`
- `servers/kinetics/tests/test_admission.py::test_the_slot_is_held_until_the_integration_finishes_not_until_the_caller_gives_up`
- `servers/kinetics/tests/test_admission.py::test_only_the_integrator_is_gated_and_it_is_gated`
- `servers/kinetics/tests/test_admission.py::test_the_ceiling_fits_inside_the_callers_budget`
- `servers/kinetics/tests/test_admission.py::test_the_ceiling_is_an_environment_variable_and_the_shipped_gate_uses_it`
