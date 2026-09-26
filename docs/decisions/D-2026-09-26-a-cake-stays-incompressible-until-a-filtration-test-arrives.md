# D-2026-09-26-a-cake-stays-incompressible-until-a-filtration-test-arrives — A cake stays incompressible until a filtration test arrives

**Status:** accepted · **Date:** 2026-09-26

## Context

`servers/unitops`' `filtration_time` takes one specific cake resistance `alpha` and assumes it is
independent of pressure. Real organic cakes compress: `alpha` rises with pressure, so the tool
overstates what pushing harder buys — in the optimistic direction, which is the one that gets a
filter under-sized. The compressible form is `alpha = alpha₀·ΔPˢ`, and the compressibility exponent
`s` is fitted over filtration tests on one slurry at **several** pressures. That data exists in
nobody's checkout in this family.

Three shapes were available: model the cake as compressible with a default `s`; take `s` as an
input now; or keep the incompressible model and say what it costs.

## Decision

**Keep the incompressible model, and ship no default compressibility anywhere.**

- **A default `s` is declined outright.** It would be this server inventing a property of the
  crystals — habit, size distribution, how the batch was cooled — and returning a number that looks
  measured. `CLAUDE.md`'s "refuse rather than approximate" is the rule: a silently substituted
  input corrupts everything downstream of it. There is no value of `s` that is a safe default,
  because the error it hides is the direction that under-sizes a filter.
- **An `s` input is declined for now**, not for ever: a parameter with nothing to fill it invites
  the model to guess one, which is the default by another route.
- **The interim is the one already shipped**: the tool's docstring and the `basis` it returns both
  say the cake is assumed incompressible and which way that errs. The `basis` is the half that
  reaches a chemist, because it travels with the number the model quotes.

**Revisit when:** filtration-test data arrives through the ELN — Chemclaw3's `ingest/sources`
ingesting a site's filtration or leaf tests where one slurry is filtered at two or more pressures,
with filtrate volume against time recorded at each. From those, `alpha` at each `ΔP` regresses to
`alpha₀` and `s`. The shape then is one tool taking `alpha₀` and `s` as inputs from that fit, never a
default — the same trigger `servers/kinetics`' absent `fit_rate_law` waits on, for the same reason.

## What keeps it true

- `servers/unitops/tests/test_tools.py::test_the_filtration_answer_says_it_computes_no_wash` — the
  filtration answer's `basis` states `INCOMPRESSIBLE`, so the assumption reaches the reader with the
  number.
- `servers/unitops/tests/test_tools.py::test_every_tool_returns_a_basis_that_names_its_model_and_its_assumption`
  — every tool's `basis` names an assumption. Nothing tests the *absence* of a default `s`: there is
  no parameter to test, and adding one is the change this record says to argue first.
