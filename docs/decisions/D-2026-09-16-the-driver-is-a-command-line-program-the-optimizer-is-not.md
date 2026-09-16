# D-2026-09-16-the-driver-is-a-command-line-program-the-optimizer-is-not — The driver is a command-line program, the optimizer is not

**Status:** accepted · **Date:** 2026-09-16 · **Commit:** geomeTRIC in `servers/calc`, and the four
unit conversions derived from `scipy.constants`. One `_HAMILTONIAN_REVISION` bump covers both.

## What was replaced

`engine/xtb_opt.py`'s in-process optimizer was about 330 lines driving
`scipy.optimize.minimize(method="L-BFGS-B")` with **both** of its own stopping tests disabled
(`gtol=0.0`, `ftol=0.0`), convergence enforced by a `StopIteration`-raising callback, over a
hand-built per-coordinate trust region and the Lindh pairwise model Hessian in `engine/anc.py`.

`anc.py`'s own docstring stated the gap in the library's favour — "a full Lindh model with angle and
torsion terms would do better, at the cost of primitive-internal machinery and a Wilson B matrix" —
and measured its preconditioner at about **2x** against ANCopt's 8-11x. It is deleted, with its
test. What replaces it is **geomeTRIC** (BSD-3) over delocalised internal coordinates, which is that
machinery, maintained, with a Wilson B matrix.

This is the live path, not a fallback: the shipped image installs `xtb` and pins
`CHEMCLAW_XTB_ENGINE=tblite`.

## The finding that shaped the integration

The obvious entry point is `geometric.optimize.run_optimizer(customengine=...)`, and it is the wrong
one. Driven with a trivial engine and measured on both sides, one call:

- writes **`<prefix>.log`, `<prefix>.tmp/` and `<prefix>_optim.xyz` into the process's working
  directory**, and
- **replaces the root logger's handlers** — `StreamHandler` becomes geomeTRIC's `RawStreamHandler`
  *and* a `RawFileHandler` — permanently, from inside a tool call.

`run_optimizer` is a command-line program wearing a function's clothes. Both effects are
disqualifying for a stateless server in this fleet: `connector_app` owns the process's log
configuration, which is why `CLAUDE.md` says in as many words not to call `basicConfig` in a server,
and a tool that writes unbounded files into its pod's working directory on every call is not
stateless in any useful sense.

`geometric.optimize.Optimize` is the optimizer underneath the driver. Driven with a
`TemporaryDirectory`, measured in both directions: it writes nothing outside that directory and the
root logger's handler list is unchanged. That is the entry point used. geomeTRIC's own per-cycle
progress logging is stopped at its own logger (`propagate = False` plus a `NullHandler`) rather than
reformatted at the root — it is a CLI progress report, and its *failures* travel as exceptions.

## What changed in the key, and why it is one invalidation

`_HAMILTONIAN_REVISION` goes `h2` → `h3`, **once**, for three changes that all move numbers:

1. **The optimizer.** A different optimizer reaches a different stationary point in the last
   decimals, and a `structure_id` is a hash of coordinates.
2. **The unit conversions.** The four literals in `xtb_engine.py` carried the comment "CODATA 2018,
   to full double precision", which was false of the first: CODATA-2018's Bohr radius gives
   1.8897261246257702 and the literal stopped at 1.8897261246. They are now derived from
   `scipy.constants` — and that makes them track the installed CODATA edition, which is a *new*
   exposure: scipy 1.17.1 ships **CODATA 2022**, so the length conversion moved by 6.9e-10 relative
   the moment it was derived, and a scipy shipping CODATA 2026 would move it again. So
   `engine_version()` now names the scipy distribution beside tblite and RDKit. Without that, a
   `pip install -U scipy` is a silent physics change served from a cache — the exact failure
   `engine/key.py` is written against.
3. **The spec's shape.** `OptSpec.curvature_floor` is gone, because the ANC preconditioner it
   floored is gone and geomeTRIC has no analogue: it builds real internal coordinates rather than
   guessing a curvature for the ones a pairwise model cannot see. That is a change to
   `model_dump()` and therefore to every optimization `params_hash`, which is why it shipped in the
   same commit as the revision bump rather than on its own. `xtb_anc_curvature_floor` is deleted
   from `CalcSettings` with it, and the knob test that named it is replaced by
   `test_the_trust_radius_moves_the_optimisation_key`, which makes the same argument about the
   optimizer knob that survived. (Replaced rather than deleted, and the replacement keeps the same
   file, because `test_the_probe_answers_in_both_directions` needs a setting that is known to reach
   a key in order to prove its own detector can say "keyed" as well as "not keyed".)

`trust_radius` **kept** its field, its key position and its meaning — the furthest one step may move
an atom — as geomeTRIC's `tmax` rather than as a per-coordinate bound. It is the ceiling on an
adaptive radius that opens at geomeTRIC's own 0.1, because starting at the ceiling gives up the
adaptivity that is half of why an internal-coordinate optimizer is fast.

`servers/calc/tests/test_key_contract.py` is unchanged in substance: the hash, the envelope, the
flat format and the four field names are a pinned cross-repository wire contract with Chemclaw3 and
none of them moved. What moved is the *content* of the `calc_version` segment, which is what a
revision bump is for, and the end-to-end pinned string was re-derived and now pins scipy too.

## What it does to the answers

Measured by running the **same five molecules through both optimizers on the same checkout of
everything else** — a git worktree at the commit immediately before this one, so tblite, RDKit,
scipy and the unit conversions are identical and the optimizer is the only difference:

| molecule | old E (Hartree) | new E (Hartree) | ΔE (kcal/mol) | old cycles | new cycles |
| --- | --- | --- | --- | --- | --- |
| water | -5.07054424 | -5.07054435 | -0.00007 | 2 | 2 |
| ethanol | -11.39432910 | -11.39433174 | -0.00166 | 9 | 6 |
| acetic acid | -14.45994252 | -14.45994244 | +0.00005 | 10 | 6 |
| benzene | -15.87964060 | -15.87964058 | +0.00001 | 2 | 2 |
| aspirin | **refused** | -39.63184461 | — | — | 29 |

Every energy that both optimizers reached agrees to better than **0.002 kcal/mol**, which is two
orders of magnitude below the calibration residual of anything built on these geometries. geomeTRIC
reaches the marginally lower value on **two** of the four — water and ethanol — and the marginally
higher one on acetic acid and benzene, by 5e-5 and 1e-5 kcal/mol; at that size the sign is which
side of the same minimum each stopped on rather than a ranking. The cycle counts are lower on the
two molecules with internal degrees of freedom to speak of and equal on the two that are nearly at
their minimum already.

**The aspirin row is the one that matters and it is not a speed figure.** The old optimizer did not
produce a worse answer on it; it produced **no** answer — `a geometry optimization exceeded this
server's inline budget of 780s (spent 782.8s)`, which is the refusal `xtb_inline_timeout_seconds`
exists to give. geomeTRIC relaxed the same molecule in 320.6 s and 29 cycles in the same contended
window. A 21-atom drug molecule is not an exotic input for this server, so that is a capability
change rather than a latency one.

**The wall clocks are not reported as a speedup**, deliberately: every run above shared a four-core
box with other work, and the per-molecule seconds swing by more than the difference being claimed
(water measured 29.8 s old against 31.7 s new; ethanol 85.5 against 66.2). What survives that noise
is the cycle count, which is deterministic, and the aspirin refusal, which is a budget boundary
rather than a stopwatch.

## The constrained path, where the default is wrong

geomeTRIC's `conmethod` defaults to `0`, its 2016 constraint algorithm, whose own source comment
says constraints are "satisfied slowly unless `enforce` is enabled". On this repository's own
`scan_point` — an ethanol dihedral driven to 60° with four atoms frozen — that default **cannot meet
this module's convergence contract**:

|  | energy | cycles | free-atom max &#124;gradient&#124; | frozen atoms moved |
| --- | --- | --- | --- | --- |
| `conmethod=0` | -11.39379720 | 12 | 1.014e-03 | 1.4e-08 Å |
| `conmethod=1` | -11.39383627 | 27 | **3.773e-04** | 0.0 Å |

against a 5.0e-04 Hartree/Angstrom target. The failure under `0` is not slack in the constraint —
both hold the frozen atoms to numerical zero — it is the **free** subspace being left unminimized,
and it is not a convergence-tightness problem either: driving geomeTRIC's own gradient target to a
quarter and then a tenth of ours moved the residual to 8.47e-04 and 8.83e-04, a plateau rather than
a descent. `conmethod=1` costs more than twice the cycles and reaches a *lower* energy, which is
what "the free subspace was not minimized" looks like from outside.

`enforce` was tried first, on the reading that the constraint was drifting. It is **not** in the
shipped code, because it turned out to change nothing measurable here: with `conmethod=1`, `0.0` and
`0.1` give the same energy, the same 27 cycles and the same 3.773e-04 residual. Upstream says the
same thing in its own comment — with the 2019 algorithm, `enforce` is unnecessary — and a knob that
is measured to do nothing is a knob this repository does not ship.

## What the convergence contract is, and why geomeTRIC's is not it

geomeTRIC requires **all five** of its criteria (energy, RMS and max gradient, RMS and max
displacement). This module promises exactly one — the largest gradient component over the atoms that
are free to move — and `_optimize_with_binary` has always re-verified that on whatever geometry a
backend returned, on the argument that "a backend that converged to its own looser threshold must
not quietly weaken that". The in-process path now does the same thing for the same reason: the
gradient and RMS-gradient criteria are set from `gradient_tolerance` (converted to atomic units,
since geomeTRIC works in Bohr), the other three are opened up so they cannot bind, and the returned
geometry is re-evaluated. A relaxation that comes back above tolerance raises, as before.

Two behaviours were preserved deliberately and are asserted rather than assumed: a structure that is
already a minimum runs **no optimizer at all** and comes back byte-identical with `steps == 0` (the
property that stops a re-optimization minting a new `structure_id` and forking every downstream
key), and the `Deadline` is checked **per gradient** rather than per cycle, because one cycle on a
large substrate is unbounded in seconds.

`steps` now means optimizer *cycles* on both backends, which is what the binary path always reported
and what the L-BFGS-B path did not — it summed `outcome.nit` across its legs.

## What keeps it true

- `servers/calc/tests/test_engine.py::test_a_converged_structure_is_a_fixed_point` — the byte-identity
  property, which is what keeps a re-optimization from forking every key built on the geometry.
- `servers/calc/tests/test_engine.py::test_frozen_atoms_are_held_exactly` — a frozen atom is held by
  a Cartesian constraint in the coordinate system, which is a stronger statement than the equal
  optimizer bounds it replaces.
- `servers/calc/tests/test_engine.py::test_a_scan_point_holds_its_coordinate_while_the_rest_relaxes`
  — the constrained case that `conmethod=0` cannot pass, which is what holds `_CONSTRAINT_METHOD`.
- `servers/calc/tests/test_engine.py::test_a_strained_start_relaxes_and_reports_what_it_was_worth`
  — the convergence promise, re-verified on the returned geometry.
- `servers/calc/tests/test_optimizer_integration.py::test_the_driver_that_writes_files_and_seizes_the_root_logger_is_not_the_one_used`
  and `test_geometric_logging_does_not_reach_the_root_logger` — the finding above, held as a
  property of this module rather than as a paragraph.
- `servers/calc/tests/test_optimizer_integration.py::test_a_relaxation_writes_nothing_into_the_working_directory`
  — the other half, driven rather than argued.
- `servers/calc/tests/test_key_covers_every_knob.py::test_the_trust_radius_moves_the_optimisation_key`
  — the surviving optimizer knob is still in the key.
- `servers/calc/tests/test_key_contract.py::test_the_whole_key_is_stable_on_the_versions_this_test_observes`
  — the wire contract, now pinned against scipy as well.
- `servers/calc/tests/test_unit_conversions.py::test_the_version_string_names_the_distribution_the_constants_come_from`
  — the control that makes a CODATA edition change a cache miss rather than a silent shift.
- `servers/calc/tests/test_unit_conversions.py::test_the_derived_conversion_is_the_published_one`
  — the four conversions against numbers nobody here computed.
