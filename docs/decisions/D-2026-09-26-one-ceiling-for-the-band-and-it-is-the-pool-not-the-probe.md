# D-2026-09-26-one-ceiling-for-the-band-and-it-is-the-pool-not-the-probe — One ceiling for chem's heavy band, derived from the pool rather than the probe

**Status:** accepted · **Date:** 2026-09-26

## What was found

`servers/chem` gated `render_structure` alone, at 8, while its worst legal depiction measures
4.6 ms — and five species tools that cost seconds had no ceiling. The backlog row owed one
measurement first: whether those tools hold the interpreter the way `enumerate_microstates`
measurably does not. Measured in the `cc3-gate` image on a loaded eight-thread host, engine CPU per
call and a 10 ms tick beside the call:

| tool | PAMAM G4 (996 atoms) | worst 1,990-atom shape | tick late, n=1 / n=4 (G4) |
| --- | --- | --- | --- |
| `describe_topology` | 5,181 ms | 18,030 ms (polyester, answered) | 2,900 / 5,714 ms |
| `enumerate_tautomers` | 3,583 ms | 19,351 ms (polyester, then refused) | 2,575 / 6,701 ms |
| `enumerate_degradants` | 2,338 ms | 47,572 ms (polyol, then refused) | 64 / 187 ms |
| `enumerate_stereoisomers` | 23 ms | 10,226 ms (polyol, then refused) | 8 / 34 ms |
| `enumerate_protonation_states` | 760 ms | 255 ms (input-bounded) | 13 / 40 ms |

Two of the five are single RDKit calls that hold the GIL for their whole duration: a legal dendrimer
stalls the process 2.9 s against a 3 s `readinessProbe.timeoutSeconds`. All five run one at a time
(n=4 costs about 4x n=1). The three other graph tools (`describe_sites`, `enumerate_torsions`,
`enumerate_bond_cleavages`) measured at most 0.55 s on the same shapes.

## What was decided

The row offered two answers: one ceiling for the band derived from the probe, or declare the render
ceiling a knob the 5-wide pod thread pool already makes unreachable. **One ceiling for the band, and
derived from the pool, because the probe arithmetic has no solution.**

- **The six heavy tools share one gate** (`engine/admission.py::GATED_TOOLS`): they spend one
  interpreter, so a gate per tool would admit twice the work onto it.
- **No N >= 1 satisfies "N x worst call under a third of the probe"**, nor "under half of
  `request_timeout`": the worst single call exceeds both on its own. That is a property of one call,
  which a ceiling bounds nothing about; only an input bound prices it, and it is queued in
  `docs/BACKLOG.md` with the table above rather than papered over with a number.
- **The ceiling is the pool width less one** (`DEFAULT_MAX_CONCURRENT_HEAVY_CALLS = 4` on
  `limits.cpu: "1"`). Admitted calls run one at a time whatever the ceiling, so a slot buys no
  throughput; a slot beyond the pool buys a queue, which the gate promises not to be. The old
  argument that renders 6-8 waiting for a worker was harmless rested on every admitted call being a
  4.6 ms render, and stops holding once a waiting call can sit behind four multi-second ones. The
  spare thread keeps the ungated tools answerable.
- **The variable is renamed** `CHEMCLAW_CHEM_MAX_CONCURRENT_HEAVY_CALLS`, and the old
  `CHEMCLAW_CHEM_MAX_CONCURRENT_RENDERS` is refused at import rather than silently ignored.

## What keeps it true

- `servers/chem/tests/test_admission.py::test_the_band_is_gated_and_nothing_else_is`
- `servers/chem/tests/test_admission.py::test_a_species_enumeration_and_a_depiction_share_one_ceiling`
- `servers/chem/tests/test_admission.py::test_the_retired_variable_is_refused_rather_than_ignored`
- `servers/chem/tests/test_depiction_bound.py::test_every_admitted_call_has_a_worker_and_the_ungated_tools_keep_one`
- `servers/chem/tests/test_depiction_bound.py::test_the_pod_pool_is_the_width_the_ceiling_was_argued_against`
- `tests/test_decision_log.py::test_every_retired_citation_names_a_live_replacement`
