# D-2026-09-19-an-atom-count-under-a-byte-cap-is-a-range-not-a-figure — An atom count under a byte cap is a range, not a figure, and a margin derived from a refused size is derived from nothing

**Status:** accepted · **Date:** 2026-09-19 · **Builds on:**
`D-2026-09-19-a-claim-about-another-repository-is-checked-by-re-reading-it` (a count in prose is a
claim about its author's afternoon), and `D-2026-09-18-a-ceiling-is-derived-from-the-pod-it-protects`,
whose figure this corrects for the second time.

## What was measured

An adversarial review of the commit that corrected `Structure`'s atom-ceiling prose found that the
correction was itself two record defects. Both are prose; neither moves a bound. That is the reason
they are worth an ADR rather than a quiet edit — the first one has now been wrong in three
successive states, each time with more confidence than the last.

### 1. `37,983 atoms ... at 26.3 bytes an atom` is not a number this server has

The sentence does not reconcile with its own coefficient: `1,000,000 / 26.3` is **38,023**. And the
coefficient is not what a payload measures. Driven against `DEFAULT_MAX_REQUEST_BYTES` with a real
`tools/call` envelope around a `Structure`:

| payload | bytes/atom | atoms under the 1 MB cap |
| --- | --- | --- |
| coordinates to 1 decimal, carbon only | 19.3 | **51,807** |
| coordinates to 3 decimals, carbon only | 23.6 | 42,394 |
| coordinates to 6 decimals, five elements | 30.4 | **32,876** |

A **1.6× spread**, set by nothing but how many decimals the caller writes. (A sampled payload of
uniformly random coordinates measures 21.5–36.6 and 27,322–46,466 over the same three shapes; the
table is the deterministic fixture the test reproduces, so the two agree on the claim and differ on
digits, which is the whole subject here.) There is no single
figure to state, and the five-digit form claims a precision the quantity does not have.

The history is the point. The sentence read `~42,000` (never measured), was corrected to `~38,000`
in three places by `D-2026-09-18`, was corrected again to `37,983` in a fourth place it had missed,
and all three states were the same defect: a range presented as a figure. What it is evidence for
is unchanged and is what the ceiling actually rests on — **every point in that range is roughly 70×
`xtb_max_atoms`**, so the transport bound does not bound the optimizer and `Structure` must.

### 2. The 120 s margin was derived from a structure this server refuses

`servers/calc/README.md` justified the tier margin with "one single point measured **81 s** here
**at 493 atoms**". `xtb_max_atoms` is **450**, so `Structure` refuses 493 before any engine sees it.
The honest figure at the size the server admits is in the same sweep: **62.7 s at 453 atoms**. The
margin is unchanged and the direction is conservative — 120 s is 62.7 plus most of itself again,
which is how to round a bound that exists because `budget.Deadline` is not checked *inside* a single
point. What was wrong was that the derivation no longer connected to the ceiling it was about.

## Decision

- The four documents state the measured **range** and name its cause (the caller's decimal places),
  rather than any figure.
- The README derives the margin from **453 atoms / 62.7 s**, and says in one line why the 493-atom
  reading in the same sweep is not the basis.
- `servers/calc/tests/test_cost_bounds.py::test_the_body_cap_admits_far_more_atoms_than_the_ceiling_at_every_formatting`
  asserts the **inequality** at three formattings — that the cap admits far more than
  `xtb_max_atoms` — because that is the claim the ceiling depends on and it is the part that does
  not go stale when somebody renames a field in the payload.

## What keeps it true

- `servers/calc/tests/test_cost_bounds.py::test_the_body_cap_admits_far_more_atoms_than_the_ceiling_at_every_formatting`
- `servers/calc/tests/test_cost_bounds.py::test_a_structure_larger_than_the_ceiling_is_refused_before_any_engine_sees_it`
- `servers/calc/tests/test_cost_bounds.py::test_the_ceiling_s_refusal_names_the_way_forward_its_own_docstring_states`
