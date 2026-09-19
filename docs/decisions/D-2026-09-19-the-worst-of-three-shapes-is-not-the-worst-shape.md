# D-2026-09-19-the-worst-of-three-shapes-is-not-the-worst-shape — The worst of three shapes is not the worst shape

**Status:** accepted · **Date:** 2026-09-19 · **Builds on:**
`D-2026-09-19-a-bound-on-the-site-count-prices-half-the-work`, whose bound this leaves unchanged and
whose cost claim it corrects.

## What was measured

That decision replaced a site-count cap on `enumerate_microstates` with a `sites × heavy atoms`
cost bound at 150,000, derived from three measured molecules: a branched dendrimer at 4.3–4.7 µs
per site-atom, a macrocycle at 5.6, a long-chain polyamine at up to 8.4. It concluded, in as many
words, that *"the bound prices the worst of the three"*.

That sentence is true. It is not the sentence the derivation needed, and the difference is a fourth
ordinary shape nobody drove: all three fixtures are **aliphatic**.

Re-measured on one container, two runs each, best of two — the three aliphatic figures reproducing
the original derivation to within a few percent, which is what makes the fourth comparable:

| shape | atoms | sites | product | best | µs/site-atom |
| --- | --- | --- | --- | --- | --- |
| branched dendrimer | 367 | 123 | 45,141 | 197 ms | 4.37 |
| chain, 100 amines | 1,401 | 100 | 140,100 | 1,194 ms | 8.52 |
| macrocycle, 223 amines | 670 | 223 | 149,410 | 1,483 ms | 9.92 |
| **oligopyridine, n=158** | 948 | 158 | **149,784** | **1,986 ms** | **13.26** |

A poly(pyridine) puts every site in a ring, so each toggle re-runs aromaticity perception over the
whole conjugated graph rather than over a chain segment. It is **1.6× the shape called the worst**,
and — unlike the three — its cost per site-atom *rises with the product*: **10.75, 11.97, 13.26 µs**
at n = 100, 130, 158. So `sites × atoms` under-prices an aromatic molecule by more the closer it
gets to this bound, which is the one place a cost bound most needs to be right.

## Decision

**The bound stays at 150,000.** Pricing this shape at the old 1.3 s would mean ~98,000, which
refuses the 223-amine macrocycle and the 100-amine chain the derivation deliberately admits — a real
regression for real polyelectrolytes, taken to make a sentence true. The sentence is what was wrong,
so the sentence is what changed: the worst call this admits costs about **2.0 s** of one core,
**1.5×** inside the readiness probe's 3 s timeout (not 2.4×) and **15×** inside the manifest's 30 s
`request_timeout` (not 24×).

**The test that was supposed to hold this drove the shape its own docstring called worst**, which
can only ever confirm that choice. `test_the_worst_call_the_bound_admits_stays_inside_the_probe_budget`
now drives an aliphatic chain *and* an aromatic backbone, names neither as the worst, and derives
the aromatic fixture's ring count from `MAX_SITE_ATOM_PRODUCT` — `sqrt(bound / 6)` — so raising the
bound moves the fixture to the new frontier instead of leaving it at the old one.

`docs/BACKLOG.md`'s admission-ceiling row is corrected in the same commit: whatever ceiling it
settles on divides 2.0 s, not the 1,266 ms it was rewritten with hours earlier.

## What this is not

A claim that a ceiling is now needed. The concurrency result that decided against one — eight
concurrent worst-legal calls leaving the event loop 384 ms late against a 3 s probe — was measured
on the aliphatic worst; scaled by 1.6× it is still far inside the budget. What the open row owes is
the four *unbounded* enumerators beside this one, which is unchanged and is where the measurement
is missing.

## What keeps it true

- `servers/chem/tests/test_microstate_bound.py::test_the_worst_call_the_bound_admits_stays_inside_the_probe_budget`
  — drives an aliphatic chain **and** an aromatic backbone against the probe budget, names neither
  as the worst, and derives the aromatic fixture's ring count from `MAX_SITE_ATOM_PRODUCT` so the
  fixture follows the bound. Driving only the shape the docstring calls worst is the defect.
- `servers/chem/tests/test_microstate_bound.py::test_a_molecule_at_the_bound_is_not_refused_by_the_bound`
  — the bound is unchanged, which is the decision here, and this is what would red if a later
  session lowered it to make the cost sentence true instead.
- `servers/chem/tests/test_microstate_bound.py::test_the_bound_prices_the_work_and_not_the_site_count`
  — the ordering the whole cost model rests on, which the aromatic shape stretches rather than
  breaks.
