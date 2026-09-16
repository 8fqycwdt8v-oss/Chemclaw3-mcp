# D-2026-09-16-a-bound-with-no-off-refuses-at-import-in-one-place — A bound with no "off" refuses at import, in one place

**Status:** accepted · **Date:** 2026-09-16 · **Commit:** `mcp_server_kit.limits.env_bound` becomes
the one way this fleet reads a resource bound out of the environment. Closes the `docs/BACKLOG.md`
row headed "Five environment-read bounds still accept a value that silently breaks the server",
including the second decision it deferred — whether the bound scan should follow one named helper.

## The row's own count was wrong, in the direction that understates it

The row said **five** in its headline, listed **six** in its body, and closed by calling them
"all seven". None of the three is the number. Re-derived on 2026-09-16 with the command the row
should have carried:

```
grep -rnE 'int\(os\.environ' packages/*/src servers/*/src --include=*.py
```

that command finds seven unguarded sites and **misses an eighth**, because it is line-oriented and
`servers/pyexec` writes the cast and the read on separate lines. The true set, at the commit before
this one:

| # | variable | site | what an invalid value did |
| --- | --- | --- | --- |
| 1 | `MCP_MAX_SMILES_CHARS` | `packages/mcp_server_kit/src/mcp_server_kit/limits.py:44` | accepted silently |
| 2 | `MCP_MAX_MOLECULE_ATOMS` | `packages/mcp_server_kit/src/mcp_server_kit/limits.py:45` | accepted silently |
| 3 | `CHEMCLAW_CHEM_RENDER_SIZE_PX` | `servers/chem/src/chemclaw_mcp_chem/engine/depiction.py:40` | accepted silently |
| 4 | `CHEMCLAW_CHEM_MAX_DEPICTION_ATOMS` | `servers/chem/src/chemclaw_mcp_chem/engine/depiction.py:52` | accepted silently |
| 5 | `CHEMCLAW_CHEM_MAX_DEPICTION_CHARS` | `servers/chem/src/chemclaw_mcp_chem/engine/depiction.py:76` | accepted silently |
| 6 | `CHEMCLAW_SAFETY_MAX_COMPONENTS` | `servers/safety/src/chemclaw_mcp_safety/engine/screen.py:74` | accepted silently |
| 7 | `CHEMCLAW_CHEM_MAX_CONCURRENT_RENDERS` | `servers/chem/src/chemclaw_mcp_chem/tools.py:72` | refused by `Admission`, naming no variable |
| 8 | `CHEMCLAW_PYEXEC_MAX_CONCURRENT_RUNS` | `servers/pyexec/src/chemclaw_mcp_pyexec/tools.py:44` | refused by `Admission`, naming no variable |

The row named **neither** of the last two. Number 7 it omits outright. Number 8 the `grep` it told
a reader to run cannot see, which is the row's shape happening to the row: a claim about a tree,
checked by a command nobody ran against it.

**Six of the eight are the dangerous half and two are merely unhelpful.** Rows 1–6 accept `0` and
every negative, so the pod starts, passes its readiness probe and then refuses every request —
`CHEMCLAW_SAFETY_MAX_COMPONENTS=0` is a hazard-screening server that answers no hazard question.
Rows 7 and 8 reach `Admission.__init__`, which does refuse below `1`; its message names the
*ceiling* ("an admission ceiling of 0 would refuse every depiction") and not the variable that set
it, so what an operator gets is a CrashLoopBackOff and a number whose source they have to guess.
Row 8 is worse than it looks for a second reason: the same number is the divisor in
`default_memory_bytes`, so which of the two failures fires first is an ordering an operator should
not have to know.

## What was built

`mcp_server_kit.limits.env_bound(name, *, default, minimum, consequence)` reads the variable, casts
it, refuses a value below `minimum` or one that is not a whole number, and returns an `int`. All
eight sites go through it, and so do the **three that already had hand-written guards** —
`CHEMCLAW_RXNLABEL_MAX_BATCH`, `CHEMCLAW_RXNLABEL_MAX_CONCURRENT_BATCHES` and
`CHEMCLAW_RXNPREDICT_MAX_CONCURRENT_PREDICTIONS`, added by
`D-2026-09-12-a-bound-that-can-be-set-to-zero-has-to-say-what-zero-means`. Eleven call sites, one
mechanism. Leaving three hand-rolled copies beside a shared helper is the shape
`D-2026-09-15-five-copies-varied-the-message-not-the-mechanism` had just finished removing one
directory over.

**It raises, and that is a deliberate departure from the rule the rest of `limits.py` follows.**
`smiles_length_error`, `atom_count_error` and `Admission.take` all return a worded reason and let
the caller raise, because they run inside a request: the reason is written for a chemist waiting on
an answer, and the caller picks the exception type the model will read. `env_bound` runs at import.
There is no request, no model and no caller — the only reader is an operator looking at a container
log — so a returned reason would have exactly one possible handler at all eleven sites, which is
the shape the Rule of Three says to inline rather than abstract. What stays per-site is the
`consequence` clause, exactly as those three keep their own refusal sentences.

**Failing at import rather than at first call is the whole point.** A pod that starts and then
refuses everything is worse than one that will not start: the first takes traffic, and the second
is a `CrashLoopBackOff` with the reason in the container log.

### The floors are not all `1`

`minimum` is declared by each call site, because only the call site knows what the number measures.
Ten of the eleven floor at `1` — the guard refuses anything *above* the bound, and every subject it
bounds (a character, an atom, a component, a slot) exists in quantities of at least one, so `0`
refuses everything.

`CHEMCLAW_CHEM_RENDER_SIZE_PX` is the exception and it is a real one: it measures a **canvas in
pixels**, not a count of things, and a one-pixel canvas is not a smaller picture. Its floor is
`rdMolDraw2D.MolDrawOptions().minFontSize`, read off the drawing library rather than transcribed.
Measured on the installed RDKit, drawing ethanol, the glyph shrinks with the canvas down to that
size and then stops:

```
size=  16 fontsize= 6.000     size=  48 fontsize= 8.084
size=  32 fontsize= 6.000     size= 320 fontsize=40.000
```

Below it there is no scale at which a depiction says which molecule it is. And the failure it
prevents is silent rather than loud: `MolDraw2DSVG(0, 0)` returns a **well-formed document** with
`viewBox='0 0 0 0'` — a picture of nothing, delivered as a success — while a *negative* size is
discarded by RDKit, which substituted its own 70x21 canvas, so the knob did nothing at all.

`CHEMCLAW_CHEM_MAX_DEPICTION_CHARS` floors at `1` and that is deliberately the weaker guard of the
two, stated rather than glossed: the smallest document this server can emit depends on both the
molecule and `RENDER_SIZE_PX` (measured, methane is 1,194 characters at a 6 px canvas and 1,411 at
320), so any constant would be a bound that drifts against the thing it bounds on an RDKit bump.
What that floor catches is the value an operator actually types.

### A non-integer is refused too, and the two per-call reads in this kit still are not

`executor.thread_pool_size` and `sessions.max_sessions` log a warning and fall back on a value that
will not parse. They are read per call and have a defensible runtime default. These eleven are read
once, before the server has accepted anything, and silently ignoring a number a deployment
deliberately set is how a deployment comes to believe in a ceiling it does not have. An empty or
whitespace-only value **is** treated as unset, matching both of those and matching how a Kubernetes
`env:` entry with no value arrives.

## The ratchet: the row's warning was right, and following one named helper is the answer

The row said converting these "would take all seven out of the ratchet that exists to watch them",
and posed the order as two decisions: whether the scan should follow a named helper, and only then
whether to share the check. Both are taken here, and the first was measured rather than argued.
`tests/test_fleet.py::numeric_env_bounds` derives every environment variable first-party code turns
into a number; run against the converted tree before `_BOUND_HELPERS` existed, it fell from **45
bounds to 34** — all eleven, including four of the five `_BOUND_ANCHORS` rows.

So `_numeric_environ_reads` now follows exactly one helper, by name. That is a coupling, and it is
survivable for one reason: `_BOUND_ANCHORS` turns it into a loud failure rather than a quiet
shrink. Driven, with `_BOUND_HELPERS` pointed at a renamed helper, **four** tests in that file go
red. With the helper followed, the derived set is 45 again and **name-for-name identical to the set
before this change** — the ratchet's coverage did not shrink, and `numeric_env_bounds`' docstring
now says which helper is followed instead of claiming that none is.

## What is deliberately not covered

`env_bound` is `int`-typed, and one bound in this fleet is not an integer: `servers/props`'
`CHEMCLAW_PROPS_MAX_TB_RATIO` is a ratio, read with `float(os.environ.get(...))`. It has the same
defect — it multiplies a boiling point in kelvin, so `0` puts the ceiling at −273.15 °C and refuses
every vapour-pressure question from a pod that started cleanly. It is left alone here rather than
swept in silently, because both obvious remedies are worse than the problem at one caller: a second
`env_ratio` is the abstraction the Rule of Three says to inline, and widening this helper to
`int | float` makes `minimum` and the return type ambiguous at eleven sites that do not need it.
The row is in `docs/BACKLOG.md` with the command that re-derives the set.

## What this is not

It is not a line-count win: the helper plus its documentation is larger than the eight missing
`if`s it replaces. The case for is that eight of eleven bounds had no check at all, and that the
three that did had written the same check three times with three different sentences.

## What keeps it true

- `tests/test_fleet.py::test_every_environment_bound_refuses_at_import_and_names_its_own_variable`
  — every `env_bound` call site in the tree, **derived** rather than listed, driven at `0`, `-1`
  and `not-a-number` through `reimported`, asserting the refusal names the variable and quotes the
  value. Driven against a mutated helper that does not raise, it fails.
- `tests/test_fleet.py::test_a_bound_at_its_own_floor_is_accepted` — the other direction, at each
  site's *own* declared `minimum`, so "refuses everything" cannot pass as "has a floor".
- `tests/test_fleet.py::test_the_bound_scan_sees_both_configuration_mechanisms` — the derived
  inventory still contains the bounds that moved behind the helper, which is what the 45→34
  measurement above says it would otherwise have lost.
- `tests/test_fleet.py::test_the_derivation_reads_the_two_spellings_it_used_to_miss` — the
  boundary, now a pair: `env_bound` is followed, an arbitrary `_env_int` is not.
- `tests/test_fleet.py::test_no_shipped_deployment_moves_a_bound_the_code_reads_from_the_environment`
  — the `_BOUND_ANCHORS` floor, which is what makes renaming the helper loud.
- `packages/mcp_server_kit/tests/test_limits.py::test_a_bound_below_its_floor_names_the_variable_the_value_and_the_floor`
  — the message carries all three, plus the default and the site's own consequence clause.
- `packages/mcp_server_kit/tests/test_limits.py::test_a_value_that_is_not_a_number_is_refused_by_name`
  and `::test_an_empty_value_is_treated_as_unset` — the two parsing decisions above.
- `packages/mcp_server_kit/tests/test_limits.py::test_this_modules_own_two_bounds_refuse_at_import`
  — the bootstrap: `limits.py` defines the helper and is also one of its callers, so the
  module-scope order has to hold.
- `servers/chem/tests/test_depiction_bound.py::test_the_render_size_floor_is_the_size_below_which_a_depiction_says_nothing`
  — both halves of the one floor above `1`, driven against RDKit: the font clamp, and the
  well-formed empty document a zero canvas returns.
- `servers/chem/tests/test_depiction_bound.py::test_the_render_size_refuses_below_its_floor_and_accepts_it`
  — `minimum - 1` and `minimum`, the one site where the fleet test's `0` and `-1` would pass on a
  floor of `1` as well.
- `servers/rxnlabel/tests/test_admission.py::test_a_bound_set_to_nothing_refuses_at_import_and_names_the_variable`
  and
  `servers/rxnpredict/tests/test_admission.py::test_the_ceiling_set_to_nothing_refuses_at_import_and_names_the_variable`
  — unchanged, and still green, which is what shows the three hand-written guards were replaced
  rather than weakened.
