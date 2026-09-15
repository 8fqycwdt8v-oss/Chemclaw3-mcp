# D-2026-09-15-the-dependency-a-catalogue-proposed-was-the-one-tool-it-could-not-carry — the dependency a catalogue proposed was the one tool it could not carry

**Status:** accepted · **Date:** 2026-09-15 · **Commit:** the `kinetics` server. Supersedes the
`kinetics` row of `MODULES.md` as it stood, which proposed five tools and named Cantera + SciPy as
the offline source.

## What the catalogue proposed, and what shipped

The row read:

> Fit a rate law to time-course data, simulate a batch/CSTR/PFR, extrapolate an Arrhenius fit, and
> produce the heat-release profile `thermalsafety` consumes.
> *Proposed tools:* `fit_rate_law`, `simulate_batch_reactor`, `simulate_cstr_pfr`,
> `arrhenius_extrapolate`, `heat_release_profile`.
> *Offline:* Cantera (BSD-3) + SciPy, both installed at build time.

Shipped: six tools, **no Cantera and no SciPy**, and neither `fit_rate_law` nor
`heat_release_profile`.

## The dependency and the tool were the same decision

Four of the five proposed tools are closed-form algebra or a fixed-step RK4 over two state
variables. **One is a regression**, and it alone is what wanted the scientific stack. So the
dependency question and the scope question were never separate: dropping `fit_rate_law` dropped
Cantera and SciPy with it, and keeping it would have made this image the third-heaviest closure in
the fleet.

The precedent against it is in the server that shipped a day earlier. `servers/thermalsafety`
hand-rolled a 200-iteration bisection rather than call `scipy.optimize.brentq`, with the reason
written beside its gas constant:

> Written here rather than imported from `scipy` so that this server's dependency closure stays the
> MCP transport and nothing else — the reason `props` gives for the same choice.

Measured across the fleet, exactly **two** servers carry numpy or scipy, and neither reason
transfers: `calc` does real numerics behind a QM binary, and `pyexec` *is* a sandbox whose product
is shipping that toolbox to the agent. `rxnpredict` — the intuitive third guess — does not: it
reaches numpy only through optional extras, and no module under its `src/` imports either.

Cantera was independently wrong for the row: it is a gas-phase mechanism package, and this is
liquid-phase process chemistry.

## The probes decided the scope, not the catalogue

Chemclaw3's `data/evals/probes/process-chemistry.yaml` asks three kinetics questions, and **none of
them carries fitting data**: pc-07 refers to a dataset without pasting it, pc-08 supplies a dose
time and a temperature, pc-09 supplies a residence time. The tools that answer them are the
closed-form ones. A `fit_rate_law` built now would have had no probe able to exercise it and no
route by which a time-course reaches a tool call.

**What would reopen it** is therefore a data path rather than a decision: a real time-course
arriving through an attachment or an ELN record. At that point the questions are whether fitting
belongs in this image or its own, and whether Cantera resolves under
`uv export --frozen --require-hashes` — `docs/BACKLOG.md` already records one image that could not.

## `heat_release_profile` is deleted rather than deferred

The row asserted a data flow: this server would "produce the heat-release profile `thermalsafety`
consumes". That coupling is the one thing in the proposal that should not be built at all.
`thermalsafety` takes calorimetry a person measured — DSC, ARC, RC1 — and a heat-release profile
computed here from *assumed* kinetics would put an estimate where that server's whole argument
requires a measurement.

The honest bridge is narrower and runs the other way: `semibatch_accumulation_profile`'s peak is an
**input** to a thermal question, saying how much unreacted reagent is present, not an answer about
what temperature it would reach.

## The Arrhenius boundary is narrow and had to be drawn explicitly

`thermalsafety::temperature_for_tmr` already extrapolates along Arrhenius. Stated precisely, because
the overlap is close enough to be quoted across: it extrapolates **q**, the specific heat-release
rate of a *decomposition*, from one reference point, inside a TMR_ad inversion, and returns a
*temperature*. This server extrapolates **k**, the rate constant of the reaction being *run*, and
returns a *rate constant*.

A decomposition's apparent activation energy from a DSC and a synthesis reaction's from a kinetic
study are different numbers about different processes. A tool that accepted either would let one be
quoted as the other, so `tests/test_arrhenius.py` asserts this module exports no name containing
*tmr*, *temperature_for*, *d24*, *criticality*, *adiabatic* or *runaway* — checked by name, because
the two modules are in different servers and no import can tie them.

## What measuring the integrator found

The semi-batch tool integrates with fixed-step RK4. Its first version guarded the feed term with
`time < dose_time_seconds`, which reads as defensive and is a defect: the integration domain **is**
the dose, so the only thing that guard ever did was let the final step's `k4` stage — evaluated at
exactly `dose_time_seconds` — see the feed switched off while `k1`-`k3` saw it on.

One step of O(h) error inside an O(h⁴) scheme. Measured against a hundredfold finer grid:

| steps | with the guard | without |
| --- | --- | --- |
| 200 | 1.7e-02 | 6.4e-08 |
| 500 | 7.0e-03 | 1.6e-09 |
| 2,000 | 1.7e-03 | 6.2e-12 |

The error fell as 1/N — **first-order convergence from a fourth-order scheme** — and the reported
peak accumulation was systematically **low**, 0.047856 against 0.047940, which is the dangerous
direction for a number a dose time is chosen from.

Two things follow, and the second is the one worth carrying:

- **No absolute tolerance would have caught it.** The answer was right to three significant
  figures; a test written to pass would have used four. Only the convergence *rate* exposed it, so
  that is what the test and the readiness probe assert.
- **Fixing it removed the need for a control.** At 2,000 steps the tool cost 8.1 ms of CPU, which is
  `chem`'s `render_structure` band — the one tool in that server gated by an admission ceiling. With
  the scheme converging properly, 200 steps agrees to 6.4e-08, eight significant figures on a number
  reported to four, at **836 µs**. The default dropped tenfold and the server no longer needs a
  ceiling. A defect fixed made the control unnecessary, rather than a control covering for a defect.

The claim in this paragraph is also the fourth unverified number this session wrote and measured:
the constant's comment first said 2,000 steps agreed to seven significant figures, from what RK4
*ought* to do. Chasing that sentence is what found the bug.

## What keeps it true

- `servers/kinetics/tests/test_reactors.py::test_the_integrator_converges_at_fourth_order_which_is_what_caught_the_bug`
  — the convergence rate, driven against the reintroduced defect (2.5x against the required 20x).
- `servers/kinetics/tests/test_reactors.py::test_a_cstr_needs_three_point_nine_times_a_pfr_at_ninety_percent_first_order`
  and `test_the_second_order_cstr_bisection_matches_the_exact_quadratic_root` — two relations the
  implementation does not contain.
- `servers/kinetics/tests/test_reactors.py::test_the_time_and_the_conversion_are_exact_inverses_at_every_order`
  and `test_a_pfr_is_a_batch_reactor_and_is_computed_as_one` — the closed forms cannot drift apart.
- `servers/kinetics/tests/test_arrhenius.py::test_this_module_returns_no_tmr_and_no_temperature`
  — the boundary against `thermalsafety`, asserted over the surface.
- `servers/kinetics/tests/test_arrhenius.py::test_a_rate_constant_that_falls_with_temperature_is_refused_by_name`
  — a negative activation energy never reaches a chemist as one.
- `servers/kinetics/tests/test_server.py::test_the_readiness_probe_refuses_when_the_integrator_loses_its_convergence_order`
  — the probe refuses on the defect that actually happened.
- `tests/test_fleet.py::test_every_server_either_bounds_its_concurrency_or_argues_why_it_need_not`
  — the 836 µs exemption, carrying its measurement.
