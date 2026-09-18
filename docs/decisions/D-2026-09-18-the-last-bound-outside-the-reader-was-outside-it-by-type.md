# D-2026-09-18-the-last-bound-outside-the-reader-was-outside-it-by-type — a ratio needs the same discipline as a count, and adding the reader without the ratchet would have traded one defect for a worse one

## Status

Accepted. Completes `D-2026-09-16-a-bound-with-no-off-refuses-at-import-in-one-place`, which moved
every *integer* bound behind one reader and left the one bound that is not an integer where it was.

## Context

That decision's sweep was by mechanism, not by coverage: it found the bounds read as
`int(os.environ.get(...))` and moved them. `servers/props`' `CHEMCLAW_PROPS_MAX_TB_RATIO` is a
dimensionless ratio, so it was spelled `float(os.environ.get(...))` and every search for the defect
walked past it.

It kept the whole defect, and a sharper version of it, because the value **multiplies** a normal
boiling point in kelvin rather than capping a count. Measured on toluene:

| `CHEMCLAW_PROPS_MAX_TB_RATIO` | ceiling | effect |
| --- | --- | --- |
| 1.8 (the default) | 417.6 °C | the shipped range |
| 1.0 | 110.6 °C | *exactly* the normal boiling point — every question above it refused |
| 0 | −273.1 °C | below absolute zero — every question refused |
| −1 | −656.9 °C | the same, further |
| `loose` | — | `ValueError: could not convert string to float: 'loose'` |

A pod set to `0` starts, passes its readiness probe, and refuses every vapour-pressure question:
the `CHEMCLAW_RXNLABEL_MAX_BATCH=0` shape one type over. The last row is the other half —
a bare traceback out of `float()` names neither the variable nor the way back, which is the
argument `env_bound`'s docstring already makes.

## Decision

`mcp_server_kit.limits.env_ratio` is `env_bound` for a `float`. **A floor and no ceiling**, unlike
`MAX_MOLECULE_ATOMS`: that one has a `maximum` because raising it re-arms an uncatchable SIGSEGV,
while raising a *sanity* ratio only loosens it — and `correlations.py` argues for exactly that
freedom, for a deployment holding a real critical temperature or asking a supercritical question
deliberately. A ceiling here would contradict the decision the knob exists to serve, so its absence
is asserted rather than described.

**Separate from `env_bound` rather than folded into it.** `int` refusing `"1.8"` is a feature of
every one of that function's call sites, so a shared parser would have to be told which type it is
reading at each of them to keep it. What the two genuinely share is the *wording*, and that is what
`_refused` holds — one sentence, because an operator meeting `CHEMCLAW_RXNLABEL_MAX_BATCH=0` and one
meeting `CHEMCLAW_PROPS_MAX_TB_RATIO=0` need the same four facts in the same order.

**The floor is 1.01 and the first draft said `math.nextafter(1.0, math.inf)`.** That is the correct
mathematical floor and a useless one: it formats as `1`, so the refusal read "1 is below the minimum
of 1", and a ratio one float above 1 leaves a liquid range of about 5e-14 K. The rule the floor
states instead is that the ceiling must sit a real distance above the normal boiling point — measured
against the lowest-boiling row in the corpus (diethyl ether, 34.6 °C), 1.0 leaves 0.0 K of headroom
and 1.01 leaves 3.1 K, and Guldberg's own estimate is 1.5, so nothing a deployment could legitimately
want is excluded.

## Consequences, including the one that nearly went the wrong way

**Making the bound safe at import took it out of the deployment ratchet, and that is a worse trade
than the defect it fixed.** `tests/test_fleet.py::numeric_env_bounds` derives its inventory from the
source and follows exactly the helper names in `_BOUND_HELPERS`. Measured: moving the bound behind
`env_ratio` took the derived set from **44 to 43**, so nothing would have stopped a Containerfile or
a ConfigMap setting it outside its floor — a silent shrink of precisely the kind that constant's own
comment is written about. `env_ratio` is in `_BOUND_HELPERS` and the variable is in
`_BOUND_ANCHORS`, which is what fails loudly if the second helper is ever renamed: it is the *whole*
of what that rename would cost.

Two further places the change reached, neither of which was obvious from the diff:

- `_declared_minimum` returned `int` and read only `int` literals, so it fell through to its own
  "no `minimum=` on this call" refusal for a call that plainly has one. It returns a `float` now and
  excludes `bool` explicitly, since `True` is an `int` in Python and is never a floor.
- `mcp_server_kit.testing.reimported` never registered the throwaway module in `sys.modules`. A
  module is not self-contained while it executes: `dataclasses` resolves a string annotation by
  looking its own class's module up there, and with `from __future__ import annotations` every
  annotation is a string. Driven on `correlations.py`, whose `VapourPressure` is a `slots=True`
  dataclass, the re-execution died with `AttributeError: 'NoneType' object has no attribute
  '__dict__'` from inside `dataclasses`, naming neither the function nor the module it was given.
  Registered under the throwaway name and removed in a `finally`, so a module that raises on
  purpose — which is what every bound test asks for — leaves nothing behind.

## What keeps it true

- `packages/mcp_server_kit/tests/test_limits.py::test_a_ratio_is_read_with_the_same_discipline_as_a_count`
  drives all three arms: under the floor, not a number, and the floor accepted.
- `packages/mcp_server_kit/tests/test_limits.py::test_a_ratio_has_a_floor_and_no_ceiling` holds the
  asymmetry with the crash bounds.
- `packages/mcp_server_kit/tests/test_limits.py::test_both_readers_refuse_a_low_value_in_the_same_words`
  compares the shared sentence as a shape, so improving the wording moves both readers or neither.
- `servers/props/tests/test_vapour_pressure_ceiling.py::test_the_ratio_that_sets_the_ceiling_refuses_a_value_that_would_answer_nothing`
  drives the real module's import at each unusable value and at its floor. Verified red against the
  bare `float(os.environ.get(...))` it replaced.
- `servers/props/tests/test_vapour_pressure_ceiling.py::test_the_ratio_can_still_be_loosened_which_is_what_the_knob_is_for`
  asserts the absence of a `maximum`, so growing one becomes a decision rather than a parameter.
- `tests/test_fleet.py::test_no_shipped_deployment_moves_a_bound_the_code_reads_from_the_environment`
  fails through `_BOUND_ANCHORS` if `env_ratio` leaves `_BOUND_HELPERS`. Verified by removing it.
- `tests/test_fleet.py::test_every_environment_bound_refuses_at_import_and_names_its_own_variable`
  and `::test_a_bound_at_its_own_floor_is_accepted` now cover this bound because the derivation sees
  it again.
