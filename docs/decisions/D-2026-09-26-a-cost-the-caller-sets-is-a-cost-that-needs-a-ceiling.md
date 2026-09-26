# D-2026-09-26-a-cost-the-caller-sets-is-a-cost-that-needs-a-ceiling — A cost the caller sets is a cost that needs a ceiling

**Status:** accepted · **Date:** 2026-09-26 · **Supersedes:** the argued absence of a concurrency
ceiling in `D-2026-09-15-the-dependency-a-catalogue-proposed-was-the-one-tool-it-could-not-carry`
("the server no longer needs a ceiling"), and the `836 µs kinetics` comparison
`D-2026-09-16-a-server-that-holds-no-data-is-a-server-that-refuses-defaults` quotes beside it.

## What changed under the argument

The 2026-09-15 record argued `kinetics` out of an admission ceiling on one figure: the semi-batch
integrator cost **836 µs** at its fixed 200-step default. That was true of a fixed step count. It
stopped being one: a fixed count reported a fast reaction as perfectly safe (RK4 diverged negative
and a clamp turned it into zero accumulation), so `reactors._steps_for_stability` now derives the
floor from the caller's rate constant, and `MAX_INTEGRATION_STEPS` was raised to 200,000 so a
realistic stiff dose is answered rather than refused.

The cost is therefore the caller's to set. Measured on 2026-09-26 (CPU time, best of five): a 1 h
dose of 5 mol into 0.1 against a co-reagent at 60 at `k = 2.5` integrates ~194,000 steps in
**0.43 s**, and a call forced to the ceiling costs **0.45 s** — five hundred times the figure the
exemption was argued on.

## The choice

- **Lower the ceiling on steps back.** Refuses doses the fix exists to answer, and the refusal a
  caller would then see is "too fast for this integrator" for a reaction that is not.
- **A per-call wall clock.** Cancelling the awaiting coroutine does not stop the worker thread, so
  the CPU keeps burning for a caller who has gone — the argument `servers/calc` already measured.
- **An admission ceiling, refused rather than queued.** Taken. Pure-Python RK4 holds the GIL, so a
  thread buys latency isolation and no throughput; N admitted integrations at the worst legal cost
  must stay well inside the kubelet probe's `timeoutSeconds` of 3. Two at ~0.45 s is under a third
  of it, so `DEFAULT_MAX_CONCURRENT_INTEGRATIONS` is 2, and the integration runs off the event loop
  so `/healthz` keeps its turns.

## `unitops` keeps its exemption, on its own measurement

The 2026-09-16 record set `unitops`' widest tool, **12.2 µs**, beside two comparisons: the 63.7 µs
`thermalsafety` is argued out of a ceiling on, and the 836 µs `kinetics` was. The second is gone.
The exemption never rested on it: `unitops` has no subprocess, no pinned thread, no list input and
no caller-sized loop — Underwood's root runs a fixed 200 halvings — so its cost cannot be set by a
caller, which is precisely the property `kinetics` lost. It stays in `CEILING_IS_ARGUED_ABSENT`
carrying its own figure.

## What keeps it true

- `servers/kinetics/tests/test_admission.py::test_a_full_pod_refuses_promptly_rather_than_queueing`
  — a full pod refuses in well under a second rather than queueing.
- `servers/kinetics/tests/test_admission.py::test_the_slot_is_held_until_the_work_ends_not_until_the_caller_leaves`
  — a cancelled caller does not free a slot its thread still burns.
- `servers/kinetics/tests/test_admission.py::test_the_integration_does_not_run_on_the_event_loop`
  — the loop answers while an integration is held.
- `servers/kinetics/tests/test_admission.py::test_the_default_ceiling_is_the_one_the_gate_was_built_from`
  — the gate enforces the documented default.
- `tests/test_fleet.py::test_every_server_either_bounds_its_concurrency_or_argues_why_it_need_not`
  — `kinetics` bounded, `unitops` argued, and nothing in between.
