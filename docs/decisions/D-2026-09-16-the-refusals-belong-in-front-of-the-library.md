# D-2026-09-16-the-refusals-belong-in-front-of-the-library — The refusals belong in front of the library

**Status:** accepted · **Date:** 2026-09-16 · **Commit:** `molmass` in `servers/thermalsafety`.
Supersedes nothing. It is the first dependency argued past that server's own
"adding a dependency here is a decision to be argued in the pull request".

## What was replaced

`servers/thermalsafety/src/chemclaw_mcp_thermalsafety/engine/oxygen_balance.py` carried seventeen
hand-transcribed atomic weights and a `([A-Z][a-z]?)(\d*)` tokenizer. Both are now `molmass`'s:
`ATOMIC_WEIGHTS` is derived from `molmass.ELEMENTS` over a seventeen-symbol allowlist, and
`molmass.Formula` does the parsing.

`molmass` is BSD-3 and has **zero required dependencies** — its `Flask` and `pandas` extras are not
installed — and that is the only reason it is admissible here. `chemicals` answers the same question
and was refused: it drags numpy, scipy, pandas and fluids into what that server's `pyproject.toml`
exists to keep the shortest closure in the fleet.

## The refusals stay, and they stay *in front*

A library that parses more than this screen has been reviewed for is not a reason to accept more.
Measured against molmass 2026.1.8, three notations `parse_formula` refuses are ones the library
answers confidently:

| written | molmass answers | what the caller meant |
| --- | --- | --- |
| `Ca(NO3)2` | 164.09 g/mol, N2O6 | the same — but a token-wise reader gets N1O3, wrong by a factor of two on the element that decides the whole number |
| `CuSO4.5H2O` | 249.68 g/mol | the same, and `Cu` is outside this screen's reviewed element set |
| `2H2O` | **deuterium oxide**, 20.03 g/mol | two waters, 36.03 g/mol |

The third is the one that decided the shape. A chemist writing a stoichiometric coefficient means a
multiplier; `molmass` reads a leading digit as an isotope mass number. That is not a bug in the
library — it is a grammar this screen has not been reviewed against — and it fails *silently*, with
a plausible number. So every refusal `parse_formula` made before still runs, and it runs before the
library sees the string. Two were added for the same reason: a leading digit, and isotope brackets.

The seventeen-element allowlist stays for the same reason and is now explicitly a policy rather than
an implementation limit — `molmass` knows all 109. An isotope symbol reaches it as `2H`, so `D2O` is
refused by the element check rather than by a character check, and the message names what it saw.

## What it moved

Every weight changed in its last decimals. Measured over the 22 formulas in that server's tests and
its published cases:

- largest molar-mass change: **+0.0084 g/mol** (NCl3 — chlorine 35.45 → 35.4529);
- largest OB% change: **-0.0123 percentage points** (methane);
- **no compound crossed a band floor**, and none came within several points of one.

OB% is `-1600·demand/mass`, so a mass change moves every reported number; the published-value test
runs at a 0.15-point tolerance, forty times the largest shift, and still passes.

**The library's table is not the IUPAC 2021 conventional one, and that is worth saying rather than
hiding.** `molmass` carries sulfur at 32.0648 and chlorine at 35.4529, which are the older standard
atomic weights rather than the conventional values IUPAC publishes for elements with a
natural-abundance interval. Two defensible tables, a real difference, and the reason the independent
six-weight check in that server's tests now runs at 0.005 g/mol rather than 0.001 — still an order
of magnitude below any realistic corruption (carbon transposed to 12.101 is 0.09 out).

`engine/selftest.CONSTANTS_VERSION` went to `1.1.0`, because an operator reading `/healthz` on two
pods needs to be able to tell a build serving one table from a build serving the other. The source
digest beside it moves on its own.

## What keeps it true

- `servers/thermalsafety/tests/test_oxygen_balance.py::test_a_notation_molmass_would_answer_is_still_refused_here`
  — the three notations above, driven through `molmass.Formula` *and* through `parse_formula`, so
  the day the library stops parsing one the test says the premise changed instead of passing for a
  new reason.
- `servers/thermalsafety/tests/test_oxygen_balance.py::test_an_isotope_symbol_is_named_rather_than_silently_weighed`
  — the refusal that comes from the allowlist rather than from a character check.
- `servers/thermalsafety/tests/test_oxygen_balance.py::test_the_table_holds_exactly_the_elements_this_screen_is_reviewed_for`
  — the allowlist is what narrows the screen, not the library.
- `servers/thermalsafety/tests/test_oxygen_balance.py::test_published_compounds_come_back_at_their_published_values`
  — the answers, against values published independently of this code, at forty times the shift.
- `servers/thermalsafety/tests/test_oxygen_balance.py::test_every_weight_in_the_table_is_a_plausible_atomic_mass`
  — six weights written here from IUPAC against the library's own table, at the tolerance the
  difference between the two tables actually needs.
- `servers/thermalsafety/tests/test_server.py::test_the_readiness_probe_refuses_when_the_atomic_weight_table_is_wrong`
  — the table is still this server's vendored corpus and the probe still breaks one and reads the
  status.
- `tests/test_fleet.py::test_the_three_answers_to_molecular_mass_agree` — the derived table
  reconciled against RDKit and against a vendored column, which is the check that would catch a
  library bump moving a weight.
