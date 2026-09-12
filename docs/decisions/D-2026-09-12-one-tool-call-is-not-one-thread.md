# D-2026-09-12-one-tool-call-is-not-one-thread — One tool call is not one thread

**Status:** accepted · **Date:** 2026-09-12 · **Commit:** wave W23, on top of `91c8f6e`. No hash is
written for this pass, for the reason
`D-2026-09-12-a-bypass-that-is-not-in-the-suite-is-not-closed` §5 gives.

## Context

`CLAUDE.md` says what a slow tool owes the fleet, and the last item is the one this record is
about:

> **a ceiling on how many of it may run at once**. That last one is not the same bound as the first
> … **And the ceiling counts what the pod spends rather than calls.**

Three servers had one — `calc`, `chem`, `pyexec`. The two *heaviest* by dependency closure did not:
`rxnpredict` runs torch inference behind a 4Gi limit, and `rxnlabel` runs RXNMapper over batches of
up to 500 reactions for a corpus drain that never stops sending them. Neither had an
`engine/admission.py`, so the aggregate load on either pod was whatever its callers happened to
send.

### What was measured

**`rxnpredict`: one tool call is one worker thread per enabled predictor.** `predict_forward_reaction`
and `predict_reaction_conditions` `asyncio.gather` over every enabled predictor, and each predictor's
`predict` does its own `asyncio.to_thread`. Driven through the real tool against six doubles that
each sleep 0.4 s: **one call finished in 0.468 s against a serial 2.4 s, with six worker threads in
flight simultaneously.** So a call-counting ceiling of two admits twelve forward passes, and the
multiplier is the deployment's own enabled-model list — a number such a ceiling would never see.

**`rxnlabel`: the RDKit path is GIL-bound and one core wide.** `_represent` over a five-species
reaction measures 2.8 ms per reaction, flat from a batch of 10 to a batch of 500 (0.028 s, 0.275 s,
1.403 s), with wall clock equal to CPU time to three digits. Raising the thread count lowers
throughput: 1, 2 and 4 threads labelling 50 reactions each gave **581, 425 and 418 reactions/s** at
0.87x, 1.04x and 1.14x of one core. That is the same finding `servers/chem` measured for depiction
and Chemclaw3 measured for fingerprinting.

**Neither image pins its inference thread width, and that is worse than `calc`'s case.** `calc`'s
Containerfile sets `OMP_NUM_THREADS=1` and then hands CREST a scrubbed environment telling it to use
four — a number someone chose, which the old ceiling failed to *count*. Here nothing is set at all,
and `torch.get_num_threads()` is sized from the machine's physical cores rather than from the
container's cgroup. So a pod limited to two cores on a 64-core node hands one forward pass 64
runnable threads. It is not a number anybody chose; it changes with the node.

## Decision

**An `engine/admission.py` in each, in the shape `servers/calc` established, charging cores rather
than calls.**

- **A slot is a core.** `acquire(what, cost)` with the same clamp `calc` uses: a cost above the whole
  ceiling runs the pod exclusively rather than being permanently unadmittable, because a tool that
  can never be admitted is a tool configuration has deleted.
- **`rxnlabel` charges `mapping.inference_threads()`** — 1 with no mapper, which the measurement
  above shows is the honest cost of the RDKit path, and torch's configured intra-op width where one
  is installed.
- **`rxnpredict` charges fan-out times thread width.** An ensemble costs
  `len(enabled predictors) x inference_threads()`; a single-model call costs `inference_threads()`.
  At the shipped ceiling that makes a full ensemble exclusive on the pod, which is the intended
  answer rather than a side effect: two ensembles over five models is ten forward passes on two
  cores, every one slower than it would have been alone, and the second caller's answer is not worth
  the first caller's latency.
- **The fan-out is read from the deployment's enabled list, not from the caller's `models`
  argument.** A charge a caller can lower by naming fewer models is a ceiling a caller can walk past,
  and the exposure this gate bounds is the pod's rather than one request's.
- **The thread width is read at call time from torch**, not assumed and not written down. A
  deployment that pins `OMP_NUM_THREADS` is then charged what it pinned, without either module
  knowing that it did.
- **Both ceilings default to the pod's own `limits.cpu`**, which is 2 in both Deployments, and each
  server's `test_admission.py` reads that file rather than transcribing it —
  `servers/pyexec` shipped exactly this coupling as two transcribed copies, where lowering
  `limits.cpu` would have put two runs on one core with the suite green.

**`MAX_BATCH` becomes `CHEMCLAW_RXNLABEL_MAX_BATCH`.** It was a bare `500`, which is a bound nobody
can loosen for a genuinely larger drain without editing code and — just as importantly — one the
fleet's ratchet over what a deployment can move could not see.

**`servers/chem` gets the direct test it never had.** Its ceiling shipped covered only through
`test_depiction_bound.py`, which is about the *arithmetic* — where the number comes from and how it
relates to the probe budget and the thread pool — and touches the gate only through those numbers.

## Consequences

- **The gated set is derived from the served surface in all three servers, and the rule is not
  `calc`'s.** There, gated equals the manifest's `state_changing` list. That cannot carry over:
  `render_structure` is `read_only` and correctly so, because drawing a molecule changes nothing.
  Cost and mutability are different axes, and reusing one list for both would either gate the
  enumerations (free) or ungate the one tool that holds the interpreter for 97 ms. So each server
  names the *ungated* exceptions and derives the rest — `labeller_version` in `rxnlabel`, for
  `calculation_key`'s reason; `list_available_models` and `classify_reaction` in `rxnpredict`,
  because neither reaches a worker thread and the first is how a caller learns *why* it was refused.
- **`Admission` now exists in five servers as five copies.** That is the fleet's stated rule — one
  server never imports another, and each refusal has to name its own tool and knob, which genuinely
  differ (`chem`'s names a replica because raising its ceiling cannot help). Five is past the point
  where "copied, not imported" should be re-argued rather than repeated, and a `docs/BACKLOG.md` row
  carries that question with the anchors.
- **What is *not* decided here: pinning `OMP_NUM_THREADS` in the two images.** It would make a slot
  honestly one core and let both ceilings mean more than "one call at a time", and it is the obvious
  companion to this change. It is also a latency change to inference this session could not measure
  — torch is an optional extra no test environment here carries — and this repository does not ship
  performance changes it has not measured. The cost model above is correct either way: an unpinned
  pod is charged what it actually spends and goes serial, which is the safe direction. A backlog row
  carries it.

## What keeps it true

- `servers/rxnpredict/tests/test_admission.py::test_one_ensemble_call_is_one_worker_thread_per_enabled_predictor`
  — the measurement the whole cost model rests on, driven through the real tool: peak concurrency
  equals the predictor count, and the wall clock is a fraction of serial.
- `servers/rxnpredict/tests/test_admission.py::test_a_full_pod_refuses_the_next_prediction_before_starting_it`
  and `servers/rxnlabel/tests/test_admission.py::test_a_full_pod_refuses_the_next_batch_before_starting_it`
  — the driven saturation probes. "Promptly" is measured against work held open indefinitely, so a
  queue could not answer at all, and peak worker concurrency is asserted beside the timing because a
  refusal that still started the work would satisfy the clock.
- `servers/chem/tests/test_admission.py::test_a_full_pod_refuses_the_next_render_before_starting_it`
  — the same probe for the server whose gate had no direct test.
- `servers/rxnlabel/tests/test_admission.py::test_a_batch_is_charged_the_mappers_threads_rather_than_one_call`
  and `servers/rxnpredict/tests/test_admission.py::test_a_prediction_is_charged_the_models_threads_rather_than_one_call`
  — a slot is a core, driven with a stub `torch` so what is asserted is the number the code *reads*
  rather than this runner's core count.
- `servers/rxnlabel/tests/test_admission.py::test_the_slot_is_held_until_the_work_finishes_not_until_the_caller_gives_up`,
  `servers/rxnpredict/tests/test_admission.py::test_the_slots_are_held_until_the_work_finishes_not_until_the_caller_gives_up`
  and `servers/chem/tests/test_admission.py::test_the_slot_is_held_until_the_render_finishes_not_until_the_caller_gives_up`
  — the half that breaks the retry loop.
- `servers/rxnlabel/tests/test_admission.py::test_every_labelling_tool_is_gated_and_only_the_version_probe_is_not`,
  `servers/rxnpredict/tests/test_admission.py::test_every_predicting_tool_is_gated_and_only_the_two_that_offload_nothing_are_not`
  and `servers/chem/tests/test_admission.py::test_only_the_depiction_is_gated_and_it_is_gated` — the
  coverage rule, derived from the served surface.
- `servers/rxnlabel/tests/test_admission.py::test_the_ceiling_is_the_pods_own_core_count` and
  `servers/rxnpredict/tests/test_admission.py::test_the_ceiling_is_the_pods_own_core_count` — the
  Deployment read rather than transcribed.
- `servers/rxnlabel/tests/test_admission.py::test_both_bounds_are_environment_variables_and_not_constants`
  — `MAX_BATCH` and the ceiling, both movable from outside the image and therefore both inside
  `tests/test_fleet.py::test_no_shipped_deployment_moves_a_bound_the_code_reads_from_the_environment`.
- `servers/chem/tests/test_admission.py::test_a_gated_tool_still_advertises_its_real_signature` —
  `functools.wraps`, without which every gated tool's schema becomes `(*args, **kwargs)`.
