# D-2026-09-16-a-refusal-set-with-a-hole-in-it-is-not-a-refusal-set — A refusal set with a hole in it is not a refusal set

**Status:** accepted · **Date:** 2026-09-16 · **Commit:** the brace refusal. Extends
`D-2026-09-16-the-refusals-belong-in-front-of-the-library`, which put this blocklist in front of
`molmass` and enumerated two of the three bracket pairs.

## What was measured

```
parse_formula("{H2O}2")  -> {'H': 4.0, 'O': 2.0}
parse_formula("(H2O)2")  -> FormulaError: uses parentheses, which this parser refuses …
parse_formula("[H2O]2")  -> FormulaError: uses isotope or group brackets, which this parser refuses …
```

`parse_formula`'s docstring promised "no nesting, no parentheses", and its `Raises:` clause listed
the notations it refuses. The blocklist held `(`, `)`, `[`, `]`, `.`, `·`, `+`, `-` and a leading
digit. Braces were not on it, and `molmass` expands a brace group like any other.

The **number is right** — `{'H': 4.0, 'O': 2.0}` is the correct expansion of `{H2O}2`. That is what
makes it worth fixing rather than shrugging at: one notation is silently delegated to the library
while its two siblings are refused by name, which means one notation nobody reviewed. The next brace
group is a salt rather than a hydrate, and `Ca{NO3}2` read the same way is the factor-of-two error
the parenthesis refusal exists for.

The pre-`molmass` parser refused braces for free, because its regex admitted nothing but element
symbols and digits. The blocklist that replaced it *enumerated*, and enumerations drop things.

## Two widenings kept, and written down rather than left silent

Adopting a library moves a boundary. Two others moved with this one, both benign, and neither was
recorded until now:

- **whitespace inside a formula parses.** `parse_formula("C6 H5 NO2") == parse_formula("C6H5NO2")`,
  because `molmass` ignores it. Kept: the answer is the one the chemist meant, and refusing a
  copy-pasted formula for its spaces is pedantry rather than safety.
- **an explicit zero count is refused.** `"C0"` and `"C1H0"` raise where the old parser returned a
  zero count. Kept for the opposite reason — it is the stricter direction, and an element written
  with a count of nothing is a typo rather than a composition.

## What keeps it true

- `servers/thermalsafety/tests/test_oxygen_balance.py::test_every_grouping_bracket_is_refused_and_not_just_the_two_somebody_listed`
  — driven over the three pairs as *characters*, because the defect was a missing row in a table and
  an example-by-example test is that table written a second time. Each is confirmed to be something
  `molmass` would otherwise answer.
- `servers/thermalsafety/tests/test_oxygen_balance.py::test_the_two_widenings_molmass_brought_are_the_ones_that_were_argued`
  — so a third widening arrives as a failure rather than as a discovery.
- `servers/thermalsafety/tests/test_oxygen_balance.py::test_a_notation_molmass_would_answer_is_still_refused_here`
  — the sibling this was written in the shape of.
