# D-2026-09-18-a-ceiling-is-derived-from-the-pod-it-protects — A ceiling is derived from the pod it protects

**Status:** accepted · **Date:** 2026-09-18 · **Commit:** `CHEMCLAW_XTB_MAX_ATOMS` 500 → 450, and
the derivation that produces it moved into `servers/calc/tests/test_cost_bounds.py`.

## What was open

`D-2026-09-16-the-driver-is-a-command-line-program-the-optimizer-is-not` replaced this server's
hand-rolled optimizer with geomeTRIC and deleted the ANC preconditioner. `xtb_max_atoms` had been
set against that preconditioner's dense `(3N, 3N)` model Hessian — 3.6 s at 120 atoms, 11.6 s at
240, 32.9 s and 18.7 MB at 510 — and the number was **deliberately not changed** in that commit, on
the argument that moving a bound on an unmeasured basis is worse than leaving one whose basis is
stated as retired. The `docs/BACKLOG.md` row that remained asked for the measurement: geomeTRIC's
coordinate-system build plus one optimizer cycle at 120, 240 and 510 atoms, against the same 1 MB
body cap, and then whether 500 is still the right place.

The row predicted that "the *shape* of the argument carries and none of the constants do". Both
halves are right. What it could not predict is the **direction**: the constants moved by about
fifty times, against the ceiling.

## The mechanism, which is not the one the old basis had

`_coordinate_system` builds `DelocalizedInternalCoordinates(molecule, build=True, connect=False,
addcart=True)`. `addcart=True` puts three Cartesian primitives per atom into the primitive set on
top of the bonded internals, so `nprim` is linear in the atom count — measured at **8.83 to 8.96
per atom** on linear alkanes, 8.93 on a branched one, 8.47 on polyphenylalanine and 7.89 to 7.97 on
polyglycine. `geometric/internal.py::build_dlc_0` then forms the `(nprim, nprim)` G matrix and calls
`numpy.linalg.eigh` on it **twice** — once on the submatrix, once on the whole thing for the
expected rank.

So the peak is a small multiple of one `(nprim, nprim)` matrix and the wall clock is its
eigendecomposition. Memory is **quadratic** in the atom count, time is **cubic**. The old basis was
quadratic in memory too, which is why the shape carried; the constant in front of it did not.

Measured out of process, one build per interpreter, peak RSS from `getrusage`:

| molecule | atoms | `nprim` | build (s) | peak (MiB) | peak / (`nprim`² × 8 B) |
| --- | --- | --- | --- | --- | --- |
| linear alkane C39 | 119 | 1,051 | 0.68 | 53.5 | 6.35 |
| linear alkane C79 | 239 | 2,131 | 3.73 | 213.6 | 6.17 |
| linear alkane C169 | 509 | 4,561 | 36.5 | 956.4 | 6.03 |
| polyglycine 17 | 122 | 962 | 0.52 | 46.6 | 6.60 |
| polyglycine 34 | 241 | 1,914 | 3.65 | 177.5 | 6.35 |
| polyglycine 72 | 507 | 4,042 | 26.8 | 772.7 | 6.20 |
| branched alkane 24 | 293 | 2,617 | 6.76 | 314.1 | 6.01 |

The last column is the finding stated as a mechanism rather than as a table: **about six copies of
the G matrix, flat across a 4.3× span of atom count and three molecular shapes.** Against the
preconditioner's 18.7 MB at 510 atoms, geomeTRIC's coordinate system alone is **51×**.

## One optimizer cycle, and what it is not evidence about

Driven through this module's own path — `_geometric_molecule`, `_coordinate_system`,
`geometric.optimize.Optimize` with `maxiter=1` over the real tblite gradient:

| atoms | initial single point (s) | coordinate build (s) | one cycle, 2 gradients (s) |
| --- | --- | --- | --- |
| 119 | 0.68 | 0.52 | 1.92 |
| 239 | 4.16 | 3.64 | 12.21 |
| 509 | 41.94 | 28.93 | 99.17 |

**These wall clocks were measured on a contended four-core box** (load average 6–7, two unrelated
test suites running) and are upper bounds, not clean timings. They are reported because the
*ordering* inside them survives the contention and two things follow from it, and deliberately not
used to set the ceiling:

- the coordinate-system build is **below** the initial single point at every size, so
  `budget.Deadline`'s granularity is still one single point even though geomeTRIC added a second
  uninterruptible unit between the initial gradient and the first check;
- one cycle at the old ceiling costs on the order of a minute and a half, against a 780 s inline
  budget, so a 500-atom relaxation spends the budget rather than converging. That is a real defect
  in what the ceiling's own refusal message promises — "refused rather than started and abandoned"
  — but it is **not** this decision's basis, because the ceiling is chartered on allocation and
  `xtb_inline_timeout_seconds` is what prices time. Setting a ceiling on a cycle count would need a
  cycle count, and nobody has one for a 500-atom molecule.

The memory measurements below are unaffected by the contention: `getrusage` reports this process's
own high-water mark.

## What a whole call costs the pod

One whole `relax_structure`, in a fresh interpreter, stopped after one optimizer cycle because the
peak is reached in the initial single point, the coordinate-system build and the first gradients
rather than after a hundred cycles. Absolute peak RSS:

| molecule | atoms | peak RSS (MiB) | above this process's own resident set (MiB) | / atoms² |
| --- | --- | --- | --- | --- |
| linear alkane C39 | 119 | 249.7 | 67.2 | 4.74e-3 |
| linear alkane C79 | 239 | 436.0 | 253.7 | 4.44e-3 |
| linear alkane C169 | 509 | 1,178.2 | 978.9 | 3.78e-3 |
| polyglycine 72 | 507 | 1,006.9 | 807.8 | 3.14e-3 |

The coefficient **falls** with the atom count, because part of the peak is not quadratic, so the
value near the ceiling is the one that bounds it rather than the largest of the three. The
polyglycine row is there to say which shape the coefficient is measured on: the linear alkane is
the denser primitive set and the more expensive call at the same atom count, so 3.78e-3 is the
worst of the shapes driven and not an average of them.

Two further terms, both measured:

- **A finished calculation does not give its memory back.** `_admitted` releases a slot the moment
  the work completes, so the next call is admitted against a *count* and not against the pod's
  resident set. Three sequential 239-atom relaxations in one process: resident 182.4 MiB at
  baseline, then 318.5 / 335.3 / 335.0 MiB after each call, with the process peak 430.1 / 461.8 /
  461.8 MiB. The arena is reused rather than accumulated — it converges — but the steady-state peak
  is **6% above** a cold call's, and that is what a slot costs.
- **The server's own resident set is 181.1 MiB**, measured by importing this repository's real
  `chemclaw_mcp_calc.app` — the served ASGI object and its whole import graph — and 195.2 MiB after
  one relaxation of water.

## The derivation, and why 500 failed it

`deploy/deployment.yaml` declares `limits.memory: 4Gi`. `mcp_server_kit.sessions` has already
budgeted `DEFAULT_MAX_SESSIONS × SESSION_COST_BYTES` = 56.6 MiB of it for the session backlog
(`D-2026-09-12-a-session-is-memory-nobody-counted`). `calc_max_concurrent_requests` admits four
in-process calculations at once. So:

> server resident + session backlog + `calc_max_concurrent_requests` × (1.06 × 3.78e-3 × N²)
> ≤ the container memory limit

At **N = 500**: 200 + 57 + 4 × 1,001 = **4,262 MiB against a 4,096 MiB limit**. The shipped ceiling
was over its own bound by 166 MiB, and what that costs is an OOMKill — which does not fail the one
call, it takes the pod, and with it every other connected turn including a four-hour CREST search.

The inequality solves at **N ≤ 489**. `xtb_max_atoms` ships at **450**, the largest fifty under it:
at 450 a full pod needs 3,502 MiB, 85.5% of the limit. The rounding is the point rather than
timidity — every constant in that arithmetic carries a few percent (the molecule's shape, the
allocator, the installed geomeTRIC), and a ceiling set at its own bound is the defect this record
is about, one revision later.

**What it costs a caller** is stated because it is a real narrowing: structures of 451 to 500 atoms
are now refused. Nothing measured says such a structure could have been *relaxed* — at that size
one optimizer cycle is a minute and a half against a 780 s budget — so what is lost is the ability
to spend a pod slot finding that out.

## The body cap, measured

The old prose said a 1 MB body carries ~42,000 atoms. Measured on real embedded geometries in the
`tools/call` envelope a caller actually sends: **26.3 bytes an atom** compact, **30.3** with
whitespace, so the `DEFAULT_MAX_REQUEST_BYTES` cap admits **~37,980** atoms (32,970 with
whitespace). The absolute floor — element 1, integer zero coordinates — is ~99,700.

At 37,980 atoms the model above puts one call's peak at about **5.3 TiB**. The body cap is linear
in the atom count and the allocation is quadratic, which is the whole reason a separate atom
ceiling exists, and that gap is now wider than it was, not narrower.

## What keeps it true

- `servers/calc/tests/test_cost_bounds.py::test_a_full_pod_of_calculations_at_the_ceiling_fits_the_memory_limit_it_declares`
  — the derivation itself, over the Deployment's declared limit, `calc_max_concurrent_requests` and
  `mcp_server_kit`'s session budget. Raising the ceiling or the admission count, or lowering the
  memory limit, fails here instead of in an OOMKill.
- `servers/calc/tests/test_cost_bounds.py::test_the_primitive_set_geometric_builds_is_still_what_that_bound_assumes`
  — the half of the model that can be re-derived in-process, driven through the same
  `_coordinate_system` the optimizer calls, so a geomeTRIC that built a denser primitive set turns
  the memory coefficient red rather than leaving it describing a library nobody runs.
- `servers/calc/tests/test_cost_bounds.py::test_the_margin_covers_one_uninterruptible_single_point`
  — now an inequality against the size the 81 s figure was measured at, because the cost of a single
  point rises monotonically with the atom count and the equality it replaces failed on the *safe*
  direction.
- `servers/calc/tests/test_cost_bounds.py::test_a_structure_larger_than_the_ceiling_is_refused_before_any_engine_sees_it`
  and `test_an_oversized_structure_cannot_be_smuggled_in_as_a_tool_argument` — the ceiling is on
  `Structure`, so every primitive inherits it and a JSON payload cannot get past it.
- `servers/calc/tests/test_admission.py::test_the_ceiling_is_an_environment_variable_and_not_a_constant`
  — the shipped pair, constructed twice, so this number is pinned where a reader looks for it.
- `servers/calc/tests/test_fukui_completeness.py::test_the_row_is_bounded_by_the_atom_ceiling_rather_than_by_a_slice`
  — the one response-size bound that reads the ceiling, which now projects from `xtb_max_atoms`
  rather than from the number it happened to be.
