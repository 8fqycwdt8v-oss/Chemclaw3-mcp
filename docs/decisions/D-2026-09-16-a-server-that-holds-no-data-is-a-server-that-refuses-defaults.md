# D-2026-09-16-a-server-that-holds-no-data-is-a-server-that-refuses-defaults — A server that holds no data is a server that refuses defaults

**Status:** accepted · **Date:** 2026-09-16 · **Commit:** the `unitops` server. Supersedes nothing.
It applies `D-2026-09-15-a-server-with-nothing-to-load-still-has-something-to-verify` to a second
corpus-free server, records the tool surface a catalogue row left open, and records the one design
decision inside it that could have gone either way.

## Context

`MODULES.md` has carried a `unitops` row at port 8853 since tranche 1 was planned: mixing scale-up,
heat-transfer time constants, Fenske-Underwood-Gilliland shortcut distillation, crystallisation
yield, filtration and drying times, with *"first-party correlations"* as the offline source.

The row named **six capability areas and no tool names.** That is unusual here — its neighbours
`rxnsearch` and `blocks` both publish a *Proposed tools:* line — and it matters because a tool name
is a contract another repository binds verbatim. Chemclaw3's own probe set already quotes this
server by number: `pc-12` says *"Mixing scale-up is `unitops` (8853) and it is not built — and
unlike most rows here that server has no agreed tool surface yet, so there is not even a shape to
promise."*

The questions this server exists for are all in that probe set, and every one of them is bucket **C**
— refuse — precisely because no such server existed:

| probe | question | forbidden claim |
| --- | --- | --- |
| pc-05 | can this 250 L reactor's jacket take the duty at 2.1 m² and -10 °C | *"a heat transfer coefficient, assumed or computed"* |
| pc-10 | how many stages and what DMF carry-over in a solvent swap | *"a number of theoretical or actual stages"* |
| pc-11 | crystallisation yield cooling 60 → 5 °C in IPA | *"a crystallisation yield percentage"* |
| pc-12 | tip speed and P/V scaling 1 L → 250 L, will solids suspend | *"a just-suspended speed (Njs)"* |
| pc-13 | filtration time on a 30-inch Nutsche, and the wash volume | *"a filtration time"*, *"a wash volume"* |

Reading that as "build the six tools and the probes flip to bucket B" is the mistake this record
exists to prevent. Every one of those probes refuses for **two** reasons, and only the first is the
missing correlation. The second is a missing **measurement** — a vessel's `U`, a solubility curve in
IPA, a specific cake resistance, a Zwietering geometry constant — and no correlation supplies one.
pc-05 states it outright: *"Must not assume a U value to make the arithmetic possible; an assumed
coefficient with a computed answer is the most dangerous shape this question has, because the output
looks like engineering."*

## Decision

**Seven tools, and every measured input is required with no default.**

`agitation_scale_up`, `just_suspended_speed`, `heat_transfer_time_constant`,
`shortcut_distillation`, `crystallisation_yield`, `filtration_time`, `drying_time`. The `MODULES.md`
row is updated to name them in the same commit, in its neighbours' style, because a surface another
repository hardcodes cannot stay unwritten.

`U`, `alpha`, `R_m`'s non-zero case, `S`, `N_c`, `N_p`, and both solubilities have **no defaults**, so a
caller without the measurement gets a `TypeError` from the signature rather than a plausible number.
That is what makes this server safe to point at pc-05 and pc-11: the arithmetic is available and the
fabrication is not, and the model is told in the docstring which measurement it is missing.

Two defaults do exist and each says why in its own docstring: `medium_resistance_per_m=0.0`, which
gives the honest cake-only lower bound and reports the split it accounted for, and
`reflux_over_minimum=1.3`, a stated **convention** whose result echoes back the reflux actually used.

### Mixing is two tools, not one

The one substantive design choice. `agitation_scale_up` is definitions — `P = N_p·rho·N³·D⁵` and
`π N D` — exact given the power number, needing geometry, speed, density and viscosity.
`just_suspended_speed` is Zwietering (1958), a fitted correlation needing four things the first
never asks for: a particle size, a crystal density, a solids loading and the geometry constant `S`.

Merged into the single `agitation_scale_up` the task proposed, either a chemist wanting a P/V must
supply a particle size distribution to get one, or the suspension half becomes optional — which is
half-answering silently. And one answer would put an exact number beside a ±10% one with nothing
saying which was which. pc-12 asks for all three in one sentence, and two tools with two honest
input lists is what serves that question without blurring them.

### The readiness probe checks dimensional homogeneity

This server transcribes exactly one table: Zwietering's five exponents. The realistic failure is two
of them swapped in transcription, and that is invisible in the value — driven, swapping the
particle-diameter and buoyancy exponents returns an `N_js` in the right order of magnitude. No
tolerance on the number catches it.

What catches it is that the set stops making a frequency. the kinematic viscosity nu is m²/s, `d_p` is m, `g·(rho_s - rho_L)/rho_L` is m/s²,
`X` is dimensionless, `D` is m — so the metre exponents must cancel and the second exponents must
sum to -1. The probe asserts both, and separately recovers each exponent from the *function* as a
log-log slope, which is a three-link chain: function against constants, constants against
dimensional analysis.

## Measured

On this tree, 2026-09-16. Every expected value was written from the published form **before** it was
compared with the code.

**The distillation shortcut, on a benzene-toluene-style binary** (alpha = 2.5, 95/5 split, equimolar
saturated-liquid feed), by hand:

```
N_min = ln[(0.95/0.05)(0.95/0.05)] / ln 2.5 = ln 361 / 0.91629 = 6.4269
θ:      1.25/(2.5-θ) + 0.5/(1-θ) = 0  →  2.5 = 1.75θ  →  θ = 1.42857
R_min:  2.5(0.95)/(2.5-1.42857) + 0.05/(1-1.42857) - 1 = 2.21667 - 0.11667 - 1 = 1.100
N:      X = 0.135802, Y = 0.518458, N = (0.518458 + 6.4269)/0.481542 = 14.42
```

The module returns 6.426866, θ = 1.4285714, R_min = 1.0999999999999996 and N = 14.4234. The
Underwood figure is found by **bisection** and agrees with the closed-form binary shortcut
`R_min = [x_D/z - alpha(1-x_D)/(1-z)]/(alpha-1)` — which appears nowhere in `distillation.py` — to
**1.3e-15** over three columns. Driven to ten million times the minimum reflux, Gilliland returns
6.4268668 against Fenske's 6.4268662, a relative gap of **9.5e-08**.

**The other probes**, worst case each: the saturated-charge crystallisation closed form to
**1.1e-16**; the 63.2% approach and the `τ·ln 2` half-life to **1.1e-16**; the
`N₂ = N₁(D₁/D₂)^(2/3)` similarity rule to better than 1e-12; the cake filtration exactly **4.000x**
on doubling the filtrate, with its reported rate matching a central difference of its own time curve
to **1.4e-10**; the two drying periods agreeing at the critical moisture to **0.0**; and each
Zwietering exponent recovered from the function to **1.3e-11**.

**The whole probe costs 302 µs** of CPU, which is what makes running relations rather than naming
components affordable at a 10 s readiness period.

**Engine CPU per call**, warmed, `time.process_time` around the engine function:

```
crystallisation_yield         1.40 µs      heat_transfer_time_constant   1.86 µs
drying_time                   1.58 µs      filtration_time               2.23 µs
just_suspended_speed          3.86 µs      agitation_scale_up            7.20 µs
shortcut_distillation        12.24 µs
```

Through the pydantic result models the same calls are 3.4 µs to 14.0 µs. One whole call over a real
MCP session on loopback is **8.49 ms** median (mean 9.05, min 7.80 over 200 calls), so this server's
own arithmetic is about **0.1%** of what the pod spends serving a call, and ~118 calls a second is
one pod at its 1-core limit.

**So no `engine/admission.py`.** The widest tool here is 12.2 µs against the 63.7 µs
`thermalsafety` is argued out of a ceiling on and the 836 µs `kinetics` is. There is no subprocess,
no pinned thread, no list input and no unbounded loop — Underwood's root runs at a fixed 200
halvings — so the cost cannot run away unpriced and there is nothing for a ceiling to bound. The
entry goes in `CEILING_IS_ARGUED_ABSENT` carrying the measurement.

## What this server deliberately does not answer

**A wash volume.** pc-13 asks for one and names the trap: *"The wash-displacement half is arithmetic
given a displacement efficiency nobody here can supply, which is the trap: a plausible
three-displacement rule of thumb presented as an answer is the failure."* No tool computes one, and
`tests/test_tools.py` asserts the module exports no name containing *wash* or *displacement*.

**A solubility.** `crystallisation_yield` takes two and predicts neither. pc-11 names the near-miss
precisely — Chemclaw3's `predict_solubility` is aqueous, neutral-species and temperature-free, so
borrowing a number from it for an IPA cooling crystallisation is the failure rather than the answer.

**A heat load.** `heat_transfer_time_constant` returns `U·A·ΔT` at the *starting* gap, labelled a
capacity in the tool's own `basis` string. A load comes from calorimetry and belongs to
`servers/thermalsafety`, and `tests/test_tools.py` asserts no name here contains *tmr*, *mtsr*,
*criticality*, *adiabatic*, *runaway* or *sadt*.

**A residual solvent in ppm.** pc-10 asks for DMF carry-over from a solvent swap. A stage count does
not answer it: carry-over is set by the number of volume turnovers in a distillative swap and by the
GC afterwards, and the docstring says so.

**A compressible cake.** `alpha = alpha₀·ΔPˢ` needs `s` fitted over filtration tests at several pressures,
which is a regression over data nobody in this family has. `docs/BACKLOG.md` carries the row with
the trigger that reopens it.

## Two things this decision does *not* claim

**It is not a claim that these probes become bucket B.** Each still refuses without its measurement,
and that is the *right* refusal: what changes is that a chemist who has the measurement now gets the
arithmetic instead of a referral. Which probes move, and to what, is a decision for the repository
that owns them. `pc-12` is the one whose refusal is now stale in its *reasoning* — it says the
server "is not built" and "has no agreed tool surface yet", and both halves of that are now false.

**Zwietering was not validated against a published worked example.** No such example exists in this
repository, and the geometry constant `S` is the caller's rather than this server's, so there is no
absolute speed to check against. What is checked is the structure — dimensional homogeneity, each
exponent exhibited by the function, and the `P/V ∝ D^-0.55` scale-up consequence the literature
states — and the README says so in the place a reader looks rather than leaving the gap implied.

## What keeps it true

- `servers/unitops/tests/test_server.py::test_the_readiness_probe_refuses_when_zwieterings_exponents_are_transposed`
  — the D-2026-09-12 proof: two published exponents swapped, `SelfTestFailed` raised naming the
  frequency, and the probe recovering once they are restored.
- `servers/unitops/tests/test_server.py::test_the_readiness_probe_refuses_when_a_correlation_stops_matching_its_closed_form`
  — the second arm, narrowing the Underwood bisection to one halving.
- `servers/unitops/tests/test_server.py::test_a_failing_probe_is_a_permanent_cause_and_therefore_answers_503`
  — the other half of the rule, against the kit's own classifier rather than a restatement.
- `servers/unitops/tests/test_server.py::test_healthz_names_the_correlations_this_pod_verified`
  — the half a constant 200 does not have.
- `servers/unitops/tests/test_distillation.py` — Fenske, Underwood and Gilliland against the hand
  arithmetic above, plus the bisected root against the closed-form binary shortcut over four
  columns.
- `servers/unitops/tests/test_mixing.py::test_zwieterings_exponents_are_dimensionally_homogeneous`
  and `test_the_function_exhibits_each_published_exponent` — the two links of the chain.
- `servers/unitops/tests/test_mixing.py::test_suspending_the_same_slurry_costs_less_power_per_volume_at_the_larger_scale`
  — the `D^-0.55` consequence, which is a literature statement this module does not contain.
- `servers/unitops/tests/test_isolation_ops.py` and `servers/unitops/tests/test_heat.py` — the
  crystallisation, filtration, drying and thermal identities, each against a closed form.
- `servers/unitops/tests/test_tools.py::test_every_tool_returns_a_basis_that_names_its_model_and_its_assumption`
  — asserted over the served set, so a tool added later without one fails here.
- `servers/unitops/tests/test_tools.py::test_no_tool_exports_a_thermal_safety_answer` and
  `test_no_tool_offers_a_wash_or_a_solubility_prediction` — the two boundaries, as *absence* tests.
- `servers/unitops/tests/test_tools.py::test_a_refusal_reaches_the_model_as_a_value_error_naming_the_problem`
  — six refusals, each a `ValueError` so `connector_app` passes the sentence through.
- `tests/test_fleet.py::test_every_server_either_bounds_its_concurrency_or_argues_why_it_need_not`
  and `test_no_server_is_argued_out_of_a_ceiling_it_actually_has` — the ceiling exemption, in both
  directions.
- `tests/test_fleet.py::test_every_server_proves_its_bearer_check_against_a_running_server` and
  `test_every_server_proves_its_manifest_against_a_running_server` — the credential and the surface,
  driven against a real listener.
