# D-2026-09-15-two-formulas-for-one-quantity-make-the-convention-part-of-the-answer — Two formulas for one quantity make the convention part of the answer

**Status:** accepted · **Date:** 2026-09-15 · **Commit:** the `suitability` server. Supersedes
nothing; it adds the first server in this fleet whose subject is chromatographic method performance,
and records the design constraint that shaped its whole surface.

## The gap

Separation science and analytical method work were absent from this family — and not merely
unbuilt. Audited across both repositories on 2026-09-15: no server built or proposed covered system
suitability, peak integration or method validation; `MODULES.md`'s only touch was a three-tool
`chromatography` stub at 8891 with no argued scope; and **neither repository's backlog had a row
proposing any of it.** Chemclaw3's `src/chemclaw/analytical/` had just gained acceptance criteria
and shelf-life extrapolation, which is the *result* side, and nothing answered for the instrument
that produced the result.

Meanwhile Chemclaw3's own eval corpus had been measuring the gap all along.
`data/evals/probes/analytical.yaml` carries a dozen bucket-C probes whose only passing answer was an
honest refusal, one of which pastes six replicate injections with areas, retention times and USP
tailing factors and asks what went wrong. The arithmetic a chemist wants there — an RSD over six
seven-digit numbers — is arithmetic a language model should not be doing in prose.

## The constraint that shaped the surface

USP <621> defines the plate count **twice** and the resolution **twice**:

```
N  = 16 (t_R/W)^2                    tangent (baseline) width
N  = 5.54 (t_R/W_0.5)^2              width at half height

Rs = 2 (Δt)/(W1 + W2)                tangent widths
Rs = 1.18 (Δt)/(W_0.5,1 + W_0.5,2)   half-height widths
```

The second of each pair is the first with the Gaussian width relation substituted in — a Gaussian
peak is 4σ wide between its tangents and 2√(2 ln 2) σ = 2.3548σ at half height, and 5.54 and 1.18
are what those substitutions produce. So they agree on a Gaussian peak and **diverge as it tails**,
in the unhelpful direction: measured in `tests/test_peaks.py`, the same tailing peak reads **39%
more efficient** under the half-height convention, which is the one a chromatography data system
reports without being asked.

A plate count is therefore not a number. It is a number *and a convention*, and the two cannot be
separated without making it incomparable with the limit it is being checked against. So:

- `convention` is a **required argument with no default** on every tool where USP gives two forms.
  A default would silently pick one, and the one a caller would omit is the one the data system
  already gave them under the other convention.
- Every answer repeats the convention it used, and the half-height answers carry
  `assumes_gaussian: true`.
- **Nothing converts between them.** The conversion is only valid under the Gaussian assumption
  that the divergence has already violated, so a converted number would be most wrong exactly where
  the question is hardest.

## The same fact makes the readiness probe strong

Because 5.54 was derived from 16 and 1.18 from 2, **the constants over-determine each other**. On a
Gaussian peak the two forms of each pair must agree to within the rounding in the published
constants: 5.545 published as 5.54 forces a 0.09% disagreement, and 1.1774 published as 1.18 forces
0.21%. `engine/selftest.py` checks exactly that, and the tolerances are *derived from that rounding*
rather than chosen until the test passed.

This is the difference `D-2026-09-12-a-readiness-check-that-does-not-run-the-thing-is-not-a-readiness-check`
asks for, in an unusually clean form. A probe that called each function and found a float would pass
a transposed constant. This one cannot: 5.54 → 5.45 is a 1.7% disagreement, an order of magnitude
past what the probe allows, and `tests/test_server.py` computes both figures so the tolerance is
shown to discriminate rather than asserted to.

The transcribed <621> adjustment allowances cannot be checked that way — a regulatory limit is not
derivable from anything — so they are digested instead, over the table's *values* rather than its
source file, so a comment edit does not move the digest and a changed limit always does.

## Two refusals that are the point rather than a limitation

**A gradient is refused by name.** `permitted_method_adjustment` evaluates the isocratic allowances,
and USP restricts gradient adjustment much further because a gradient's selectivity depends on the
instrument's dwell volume as well as on the method. Applying the isocratic table to a gradient would
be **permissive**, and a control whose failure mode is permissive is worse than no control. This is
the fleet's "refuse rather than approximate" rule at its most load-bearing.

**`failures: []` does not mean a run is suitable.** It means every criterion that was *declared*
passed. `tests/test_tools.py` pins the dangerous case: a peak pair resolved at 0.49 — badly
co-eluting — reports no failure when no minimum resolution was declared, and reports one the moment
it is. That is the honest behaviour, and because it is also the one a reader will misread, it is
asserted rather than left to a docstring.

## What was deliberately not built

**ICH Q2 method validation** — linearity, accuracy, LOD/LOQ, intermediate precision. Suitability
asks "did the system perform today"; validation asks "is this method fit for purpose", over separate
preparations and a different data shape. It is a different tool family, so by this repository's
central rule it is a different server; and the first tool in it that wants a t-distribution wants a
dependency this image does not carry, which is the dependency-closure half of the same rule.

The proposed `chromatography` server at 8891 stays separate for the mirror-image reason: it
*predicts* (a retention model, a gradient scouting plan) and needs a fitted model and a training
set, where this one *checks*, from numbers a run already produced, with no model at all.

## What keeps it true

- `servers/suitability/tests/test_peaks.py::test_the_two_plate_conventions_agree_on_a_gaussian_peak`
  and `test_the_two_resolution_conventions_agree_on_a_pair_of_gaussian_peaks` — the constants'
  mutual consistency, which is what the probe rests on.
- `servers/suitability/tests/test_peaks.py::test_the_two_conventions_diverge_on_a_tailing_peak_which_is_why_both_are_offered`
  — the 39% figure, so the reason for the required argument is a measurement rather than a warning.
- `servers/suitability/tests/test_server.py::test_the_readiness_probe_refuses_when_a_usp_constant_is_transposed`
  and `test_a_transposed_plate_constant_would_break_the_agreement_the_probe_checks` — the probe
  refuses, and its tolerance is shown to discriminate a transposition from the published rounding.
- `servers/suitability/tests/test_adjustments.py::test_a_gradient_is_refused_by_name_rather_than_evaluated_against_the_isocratic_table`
  — the refusal whose absence would be permissive.
- `servers/suitability/tests/test_adjustments.py::test_the_minor_component_cap_binds_the_other_way_round_from_the_obvious_reading`
  — the row whose rule is not the one a reader assumes.
- `servers/suitability/tests/test_tools.py::test_an_undeclared_criterion_is_not_checked_and_does_not_appear_in_failures`
  — the `failures: []` contract.
- `servers/suitability/tests/test_tools.py::test_the_report_introduces_no_arithmetic_of_its_own`
  — the composite agrees with the primitives it is built from.
- `servers/suitability/tests/test_precision.py::test_the_rsd_uses_the_sample_denominator_which_is_what_the_criterion_is_written_against`
  and `test_the_injection_rule_runs_the_counter_intuitive_way_round` — the n-1 denominator and both
  sides of the five-versus-six boundary.
