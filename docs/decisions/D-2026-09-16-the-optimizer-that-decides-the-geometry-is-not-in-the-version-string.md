# D-2026-09-16-the-optimizer-that-decides-the-geometry-is-not-in-the-version-string — The optimizer that decides the geometry is not in the version string

**Status:** accepted · **Date:** 2026-09-16 · **Commit:** the geomeTRIC key repair. Extends
`D-2026-09-16-the-driver-is-a-command-line-program-the-optimizer-is-not`, which adopted geomeTRIC
and added scipy to `engine_version()` in the same hunk while leaving the optimizer out of every key
this server emits.

## What was measured

At `6c6a0eb`, on the shipped stack:

```
engine_version()            -> 'tblite-0.7.0/rdkit-2026.3.5/scipy-1.17.1/h3'
'geometric' in that string  -> False
version('geometric')        -> '1.1.1'
```

`servers/calc/pyproject.toml` said the opposite in the present tense: "Its distribution version is in
`engine_version()` for the same reason tblite's is: it decides which stationary point is reached."

## Why it matters, and why it is the *one* program that had to be named

`engine/xtb_spec.py::XtbSpec.calc_version` states the rule this server is arranged around: **name
every program whose output survives into the stored payload, and no program that does not run.**
For `xtb.opt` the payload *is* geomeTRIC's output. `optimize_geometry` and `relax_structure` store a
geometry, and which stationary point they store is the optimizer's decision — the `trust_radius`
field two lines above it already carries the measurement (0.35 and 0.05 relax ethanol to different
geometries and different energies, "and a structure id is what every downstream key is built from").

So a geomeTRIC bump would have moved every optimized geometry under an **unchanged** key: stale
cache hits in Chemclaw3's `calculation_results`, and new residuals stamped into the same
`predictions` bucket as residuals from a different optimizer. The same commit added scipy to the
string, with a twenty-line comment, for a **6.9e-10** relative shift in a unit conversion.

## Where it goes: `OptSpec.calc_version()`, not `engine_version()`

The brief that opened this record said `engine_version()`. That is the wrong half, and putting it
there would have traded one half of the rule for the other. `engine_version()` is the *engine's*
string: it keys `xtb.sp`, `xtb.properties`, `xtb.fukui` and `xtb.hess` as well, and geomeTRIC runs
in none of them — a single point, a properties panel, a Fukui triple and a Hessian are all evaluated
at a geometry somebody hands them. `xtb_opt.py` is the only module in the tree that imports
`geometric` at all, which the mypy override in the root `pyproject.toml` already states as a rule.

`OptSpec.calc_version()` is therefore `CrestSpec.calc_version()`'s shape one task over: key on the
build of the thing that actually did the work. It is **conditional on the resolved backend**, exactly
as `optimize_structure` is — `for_structure(...).engine == "xtb"` goes to ANCopt inside the binary,
which `backend_version` already names, and `cache_key` resolves before it asks for a version. The
shipped image takes the in-process path either way (it installs the binary and pins
`CHEMCLAW_XTB_ENGINE=tblite`), so in every shipped configuration the string grows.

Nothing propagates it by hand: `pka.calc_version()` already folds in `opt-{relaxation_spec()...}`
and `scan.py` builds an `OptSpec`, so both pick it up with no edit.

## What it costs, and what the consumer has to do

`calc_version` is opaque to Chemclaw3 — `connectors/calc/remote.py::remote_key` takes
`key["calc_version"]` verbatim and re-derives nothing, by its own rule — so **no code change is
needed over there**. What moves is data:

- every `xtb.opt` row in `calculation_results` is a miss, which is correct: those rows do not record
  the optimizer that produced them;
- `predictions` is unique on `(calc_type, calc_version, input_hash)` and read with an exact
  predicate, so pKa residuals recorded under the previous string become unreachable and
  `calculator_trust("pka")` reports `UNCALIBRATED`, n=0, until each molecule is predicted again.
  `pka.calc_version()`'s own docstring already accepts that cost for its own widenings.

Both are the same cost `6c6a0eb` already imposed in the other direction, in the same week, by
bumping `_HAMILTONIAN_REVISION` from `h2` to `h3` — so this lands on a set that is already cold
rather than on a warm one.

`_HAMILTONIAN_REVISION` is **not** bumped: it names a change to how a calculation is set up, and
widening the version string is itself the invalidation.

## What keeps it true

- `servers/calc/tests/test_calc_version.py::test_the_optimizer_that_decides_the_geometry_is_in_the_optimization_version`
  — driven through the tools, positive on `optimize_geometry`, `relax_structure`, `scan_point` and
  `predict_pka`, negative on the three calculations that never run an optimizer.
- `servers/calc/tests/test_unit_conversions.py::test_the_engine_string_does_not_name_a_program_the_engine_does_not_run`
  — an *absence* test, so the obvious-looking repair (widen the shared string) turns red.
- `servers/calc/tests/test_key_contract.py::test_the_whole_key_is_stable_on_the_versions_this_test_observes`
  — both keys pinned byte for byte: the `sp` one proves the optimizer stays out, the `opt` one
  proves it is in.
