# D-2026-09-26-a-stiff-dose-is-integrated-by-a-stable-scheme-not-refused — A stiff semi-batch dose is integrated by an L-stable scheme, not refused

**Status:** accepted · **Date:** 2026-09-26 · **Supersedes in part:**
`D-2026-09-26-a-tool-that-runs-on-the-event-loop-cannot-be-gated`'s "the cheaper integrator the row
offered is not taken here". Its gate, its offload and its ceiling stand unchanged.

## What was found

`servers/kinetics`' `semibatch_accumulation` derived an RK4 step count from the problem
(`_steps_for_stability`) and **refused** any dose needing more than `MAX_INTEGRATION_STEPS` =
200,000 as mixing-limited. That refusal was set by what an *explicit* scheme can afford, not by the
chemistry: the threshold is `stiffness x dose time > 557,000`, and the ordinary worked dose (an acid
chloride into an amine over two hours) crossed it at `k = 86`, the review's stiff dose at
`k = 2.58`. A reaction that fast is the *easy* case for the model — the feed reacts as it arrives
and the accumulation sits on its quasi-steady value — and an explicit step is the one tool that
cannot follow it cheaply.

## What was decided

- **Past the RK4 ceiling, and only there, the dose is integrated by Alexander's three-stage,
  third-order, L-stable, stiffly accurate SDIRK**, at a fixed `STABLE_INTEGRATION_STEPS` = 2,000.
  Below the ceiling RK4 answers exactly as before, so no answer the server already gave moves.
  Switching lower would cut the worst RK4 call's cost, and was not taken: it would replace a
  fourth-order answer with a third-order one across a band nobody asked to change.
- **Each implicit stage is one scalar equation, solved inside a bracket.** The feed enters only the
  dosed reagent and consumption is one-to-one, so `Y_d - Y_c` is known before the solve; the rest
  is monotone in `Y_d` with its root between the point either species reaches zero and the no-
  reaction value. Safeguarded Newton cannot diverge or go negative; measured, three iterations a
  stage. Pure Python, no new dependency.
- **Two things the textbook scheme got wrong here, each measured and fixed.** (1) The SDIRK
  stability function is negative at large `h*lambda`, so its first step from zero overshot the
  plateau and the overshoot became the reported peak — 0.78% high at `k = 200` with a zero-order
  co-reagent. The first step is four backward-Euler sub-steps instead (Rannacher's start), whose
  stability function lies in (0, 1). (2) A co-reagent running out inside a step left it at
  -0.011 mol and the peak 1.1e-04 low; the state is moved back onto `n_d - n_co = F*t - n_co,0`,
  which every Runge-Kutta scheme conserves, making the half-dose excess exact. That is a projection
  onto a conserved line, not the clamp `_steps_for_stability` forbids: the clamp discarded moles.
- **The mixing-limited warning survives as a caveat on the answer.** `AccumulationResult` gains
  `method` (`rk4` or `sdirk3-l-stable`) and `caveat`, which on the stable path says the reaction
  can consume the vessel's contents N times over the dose, that the perfectly-mixed number is a
  floor there, and to size the dose from the heat-removal duty (`thermalsafety`). Only a stiffness
  that is not a finite double is still refused.

## Measurements

Accuracy at 2,000 steps against RK4 on every fixture RK4 answers: worst 2.6e-07 (worked dose,
`k = 50`). Past the ceiling: 9.3e-08 against a 465,000-step RK4 run at `k = 200`; 1e-9 against the
closed-form quasi-steady limit at `k = 1e6`. Third order on a smooth dose (7.8x per doubling).

CPU per call, median of five, before and after, on a loaded 4-core x86_64 laptop:

| dose | before | after |
| --- | --- | --- |
| worst legal RK4 call (`k = 2.578`, 199,946 steps) | 1,292 ms | 1,272 ms (unchanged path) |
| `k = 2.6`, the first dose past the ceiling | refused | 82 ms |
| worked dose at `k = 200` | refused | 76 ms |
| second order, `k = 1e9`, `n_co = 2` | refused | 89 ms |
| `k = 1e12` | refused | 60 ms |

So the worst legal call is still the RK4 one `engine/admission.py` sized its ceiling on, and the
newly answered band costs a fixed ~0.1 s whatever the rate: the ceiling of three holds without
re-derivation.

**A finding this change did not fix, queued.** RK4's floor puts `h*lambda` exactly on its
real-axis stability limit when the stiffness bound is tight (first order in the dosed reagent,
zero order in the co-reagent), where the amplification factor is ~1 and the start-up transient
barely decays: measured up to 0.21% low. It is in `docs/BACKLOG.md`.

## What keeps it true

- `servers/kinetics/tests/test_reactors.py::test_a_dose_past_the_explicit_ceiling_is_answered_by_the_stable_scheme`
- `servers/kinetics/tests/test_reactors.py::test_a_dose_rk4_can_integrate_is_still_integrated_by_rk4`
- `servers/kinetics/tests/test_reactors.py::test_the_stable_scheme_agrees_with_rk4_on_every_fixture_rk4_answers`
- `servers/kinetics/tests/test_reactors.py::test_the_stable_scheme_converges_at_third_order_on_a_smooth_dose`
- `servers/kinetics/tests/test_reactors.py::test_a_dose_that_reacts_as_it_arrives_lands_on_the_quasi_steady_limit`
- `servers/kinetics/tests/test_reactors.py::test_a_co_reagent_used_up_mid_dose_leaves_exactly_the_excess`
- `servers/kinetics/tests/test_reactors.py::test_the_start_of_a_stiff_dose_does_not_overshoot_into_a_false_peak`
- `servers/kinetics/tests/test_reactors.py::test_the_stable_scheme_costs_a_fixed_step_count_however_fast_the_reaction`
- `servers/kinetics/tests/test_server.py::test_a_stiff_dose_is_answered_on_the_wire_with_its_method_and_caveat`
