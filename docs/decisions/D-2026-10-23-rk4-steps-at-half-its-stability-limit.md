# D-2026-10-23-rk4-steps-at-half-its-stability-limit — RK4's step floor sits at half its stability limit, and the step ceiling stays where it was

**Status:** accepted · **Date:** 2026-10-23 · **Supersedes in part:**
`D-2026-09-26-a-stiff-dose-is-integrated-by-a-stable-scheme-not-refused`'s "below the ceiling RK4
answers exactly as before", and the finding it queued. Its stable scheme, its start and its
projection stand unchanged.

## Context

`servers/kinetics`' `_steps_for_stability` set RK4's step count so that `h*lambda` landed exactly
on `RK4_REAL_STABILITY_LIMIT` (2.785). There RK4's amplification factor is 0.9996: stable, and
undamped. The dose starts at zero accumulation, so its start-up transient decays in about 2,300
steps instead of one, and a dose that needs only a few thousand comes back low — the dangerous
direction for a number a dose time is chosen from. The queued row put it at "0.21% low at `k = 10`".
Measured across the whole floor-bound band it is two orders worse.

A sweep of five families — the worked and the stiff geometry at first order in the dosed reagent and
zero or first order in the co-reagent, and the second-order geometry — with `k` at four points a
decade, every dose with `stiffness x dose time` from 50 to 300,000. The reference is the closed form
where the dose is linear (zero order in a co-reagent that never runs out:
`peak = (1 - exp(-k*t)) / (k*t)`), elsewhere RK4 at 20 times the floor's step count.

| | worst error over the sweep | worst legal call |
| --- | --- | --- |
| as shipped (`h*lambda` on the limit) | **-34%** (stiff, zero order, `k = 0.18`); worse than -1% from `Da` 640 to 23,000 at zero order | 199,946 RK4 steps, 1.16 s |
| A1: margin of 2, step ceiling unchanged | 3.0e-07 | 199,946 RK4 steps, 1.26 s |
| A2: margin of 2, step ceiling doubled | 3.0e-07 | 399,891 RK4 steps, 2.29 s |
| B: floor-bound doses to the stable scheme | +4.4e-03 (worked, zero order, `k = 1`); above 1e-4 from `Da` 6,400 to 40,000 | 2,000 SDIRK steps, 0.08 s |

`Da` is `stiffness x dose time`. CPU is the median of three, per call, on a loaded 4-core x86_64
laptop. At first order in the co-reagent the stiffness bound is loose, `h*lambda` sits inside the
limit anyway, and the shipped scheme was within 2e-06 throughout: the defect is where the bound is
tight.

## Options

1. **A1: put the floor at half the limit (`RK4_STEP_MARGIN` = 2) and keep `MAX_INTEGRATION_STEPS`.**
   The factor is 0.28 there and the transient is gone in a few steps. A floor-bound dose costs twice
   the steps; RK4's band ends at `Da` 278,500 instead of 557,000, and the doses between go to the
   stable scheme, which measures 3e-08 to 3.2e-07 there. The worst legal call is the same step count.
2. **A2: the same margin with the ceiling doubled to 400,000.** The same answers, RK4's band where it
   was, and twice the worst call: `engine/admission.py`'s ceiling of three (`floor(15 s / (2 x 2 s))`)
   would fall to one.
3. **B: route every dose the floor binds to the stable scheme.** Cheapest — a fixed 0.08 s — and
   nearly two orders closer than what shipped, but at 2,000 steps its backward-Euler start and third order
   leave up to 0.44% across exactly the band the defect is in.

## Decision

A1. It is as accurate as A2 for the same worst call as shipped, and B is four orders less accurate
where it matters. A2 buys nothing A1 lacks: the band it keeps on RK4 is one the stable scheme already
answers to 3.2e-07, and it would cost the pod two thirds of its admitted integrations.

## Consequences

- An RK4 dose the stiffness sets now takes twice the steps (`Da` 100,000: 72,000 steps, ~0.5 s); a
  dose the caller's 200 steps already damp is unchanged. The admission ceiling and
  `WORST_INTEGRATION_SECONDS` stand: the worst legal call is still 199,946 RK4 steps.
- Doses from `Da` 278,500 to 557,000 answer with `method: sdirk3-l-stable` and the mixing-limited
  `caveat` where they had `rk4` and none. No tool docstring or field description changed.
- The floor-bound band's answers move up, by as much as a third of the true value at zero order in
  the co-reagent.

**Revisit when:** a dose family is found whose transient matters at `h*lambda` = 1.39;
`test_a_dose_on_the_step_floor_lands_on_the_closed_form` is the check to extend with it.

## What keeps it true

- `servers/kinetics/tests/test_reactors.py::test_a_dose_on_the_step_floor_lands_on_the_closed_form`
- `servers/kinetics/tests/test_reactors.py::test_a_dose_rk4_can_integrate_is_still_integrated_by_rk4`
- `servers/kinetics/tests/test_reactors.py::test_a_dose_past_the_explicit_ceiling_is_answered_by_the_stable_scheme`
- `servers/kinetics/tests/test_reactors.py::test_the_stable_scheme_agrees_with_rk4_on_every_fixture_rk4_answers`
- `servers/kinetics/tests/test_admission.py::test_the_ceiling_fits_inside_the_callers_budget`
