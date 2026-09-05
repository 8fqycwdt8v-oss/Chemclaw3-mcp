# `calc` — the physics behind Chemclaw3's calculators · port 8860

Twenty tools, and **no model reads any of them**: Chemclaw3 keeps its own `calc` bundle and its
own agent-facing surface, and calls this server from inside `cached_compute` and from Temporal
activities. **Ten** back its SMILES-in tools one for one, **seven** are structure-in primitives its
durable-job activities compose, and **three** are helpers that compute nothing. No cache, no
artifact store, no calibration ledger, no job records, no resumption, and no network call at any
point — Chemclaw3 keeps orchestration and the cache; this server holds the physics.

**Ten tools backing Chemclaw3's own**, a SMILES in and a chemist's answer out. They keep its
model-facing docstrings word for word, so a divergence between what the two claim shows up in a diff
rather than only in an answer:

| Tool | What it computes |
| --- | --- |
| `compute_xtb_energy` | GFN2-xTB single-point total energy (Hartree). |
| `compute_electronic_properties` | HOMO/LUMO/gap (eV), dipole (Debye), Mulliken charges, Wiberg bond orders and per-atom Wiberg/free valence. |
| `predict_site_reactivity` | Condensed Fukui indices (f⁻/f⁺/f⁰, dual descriptor, local softness, local electrophilicity) plus the conceptual-DFT global panel, atoms ranked by susceptibility to attack. All of it from the same three single points — the global panel is the ion *energies* the Fukui path used to compute and discard. |
| `compute_atomic_descriptors` | Per-atom polarisability, C6, covalent coordination number and atomic multipoles, plus the electrostatic-potential extrema on request. **Binary only** — tblite exposes none of these — and it refuses by name where no `xtb` is installed rather than returning nulls. |
| `compute_surface_potential` | The most positive and most negative electrostatic potential on the molecular surface (kcal/mol) — the maximum is the acidic patch or a halogen's σ-hole, the minimum a lone pair or π face. Extrema over a grid, not a map. **Binary only**, on the same terms as the panel above, and keyed apart from it because an `--esp` run cannot also produce the atomic multipoles. |
| `optimize_geometry` | Relaxation to a stationary point of the GFN2 surface. |
| `predict_pka` | pKa of the most acidic O-H/S-H site, or a base's conjugate acid (pKaH). |
| `predict_solubility` | Aqueous log S from the ESOL (Delaney 2004) baseline, with a domain check. |
| `predict_logd` | pH-dependent logD, for singly-ionisable molecules only. |
| `predict_developability_profile` | MW, cLogP, TPSA, H-bond counts, sp3 fraction, QED, Ro5/Veber flags. |

**Seven primitives**, structure-in, for Chemclaw3's durable-job activities to compose:

| Primitive | What it computes |
| --- | --- |
| `relax_structure` | An optimisation that hands the coordinates back, with atoms optionally frozen. |
| `compute_properties_at` | One SCF at a given geometry — energy, orbitals, charges, bond orders. |
| `compute_fukui_at` | The Fukui ranking at a given geometry — the twin of `predict_site_reactivity`, sharing its `xtb.fukui` row, because a flexible molecule's site ranking is a property of the conformer and only the caller holds those. |
| `compute_hessian` | Second derivatives plus dipole derivatives, as base64 `.npy`. |
| `scan_point` | Drive an internal coordinate, freeze it, relax the rest. One point of a profile. |
| `search_conformer_ensemble` | CREST conformer / tautomer / protomer sampling. |
| `search_binding_modes` | CREST non-covalent search over a combined pair. |

**Three helpers** that compute nothing: `embed_structure` and `combine_structures` build the
geometries the primitives consume, and `calculation_key` answers what a calculation *would* be
stored under.

## The seam: Chemclaw3 keeps orchestration and the cache; this server holds the physics

Not "fast things move and slow things stay" — this server runs CREST searches that take hours. The
line is **statelessness**, and the structural test for it is whether a calculation's key can be
derived from its arguments:

- **It can** → the caller can ask "have I computed this?" before paying, so it is a primitive and it
  belongs here.
- **It cannot, because the key names an output** → it is a loop with state. That is a durable job,
  and it stays in Chemclaw3.

### Why `compute_thermochemistry` is not here

It was ported, and then removed, and the removal is the clearest statement of the rule. Four
independent reasons pointed the same way:

1. **It failed the fleet's own runtime expectation and needed the rule bent** — a 900 s timeout and
   a 150-atom cap on a fleet whose norm was ~20 s. A tool that needs an exception written for it is
   usually on the wrong side of the line.
2. **It is orchestration, not request/response**: optimise → Hessian → displace along the imaginary
   mode → repeat is a loop with state.
3. **Its key names an output.** `calculation_key` could not derive one, because the key names the
   geometry the refinement loop *finally settled on*. That was the structural signal, and it agreed
   with the other three.
4. **The measurement.** Repeating `compute_thermochemistry` in Chemclaw3 costs **0.007 s against
   0.816 s** cold for ethanol and **0.012 s against 3.273 s** for ethyl acetate — two orders of
   magnitude, on tiny molecules, entirely from the *nested* `xtb.opt` and `xtb.hess` caches.
   Shipping it here uncacheable would have converted every repeat into a full recompute, on the most
   expensive tool in the set. That is a D-011 violation in substance if not in letter.

**The answer was not to keep it in Chemclaw3 but to stop shipping composites.** Chemclaw3 assembles
the same result from `relax_structure` + `compute_hessian` + its own RRHO partition functions — and
every part of that caches, so the two-orders-of-magnitude repeat still hits. `engine/xtb_thermo.py`
is gone from this server; the RRHO arithmetic lives where it always did.

The same argument applied to everything else the durable jobs needed:

| Chemclaw3 engine | What moved here | What stayed, and why |
| --- | --- | --- |
| `xtb_scan.py` | `scan_point` — drive, freeze, relax | The sweep. A profile is a loop, and its relative energies and barrier maximum are arithmetic over the points. The decomposition is *exact*: `run_scan` already drove every point from the input geometry rather than the previous one, so the points were independent by construction. |
| `conformers.py` | `search_conformer_ensemble` — the CREST call | Boltzmann populations, conformational entropy, the ensemble free-energy correction, and `max_members` truncation. All arithmetic over the energies and degeneracies returned. |
| `complexes.py` | `search_binding_modes` + `combine_structures` | The interaction energy: three `relax_structure` calls and a subtraction, each separately cached — where Chemclaw3's single `xtb.complex` row recomputed every monomer whenever the separation changed. |
| `reaction.py` | **nothing** | It is pure composition over primitives already exposed: per-species optimise + Hessian, stoichiometric sums, and a formula-balance check. Adding a tool for it would be adding a composite back. |
| `progress.py` | **nothing** | A progress callback needs somewhere to report *to*. A request/response tool has no such channel, and inventing one would mean holding job state. |

**A CREST search is the one thing exposed whole**, and it is not an exception to the rule but an
application of it: a metadynamics run is a single trajectory, its intermediate structures are not
answers, and there is no point at which half of it is a result. One tool, one key, minutes to hours.

## Read this first: it is a backend, not a connector Chemclaw3 dials

`chem` and `safety` are complete ports, so registering them on `CHEMCLAW_CONNECTORS_DIR` swaps one
implementation for an identical one. **This server is different, and putting it on that path is
wrong** — so it is not reachable from there any more. Its manifest is registered in
`manifests-internal/`, which no published `export` line names, and declares `mount: backend`, a key
Chemclaw3's `extra="forbid"` manifest model refuses; a deployment that points a path at that
directory anyway fails at startup naming the file rather than serving a reduced surface.

Chemclaw3 keeps its `calc` bundle and every tool in it. What moved here is the *computation*
behind them; six of that bundle's tools have no computation to move at all, because they **are**
the state:

| Stays entirely in Chemclaw3 | What it is |
| --- | --- |
| `report_measurement`, `calculator_trust`, `calculator_outliers` | the calibration ledger |
| `find_calculations` | a query over the calculation cache |
| `list_artifacts`, `fetch_artifact` | the content-addressed artifact store |
| every `jobs:` entry | solvent screens, conformer ensembles, reaction energetics, relaxed scans, host–guest complexes, on Temporal |

The name is still `calc`, because this repository requires the directory, the package suffix and the
manifest `name` to be one string (`tests/test_fleet.py`). So registering this directory as a
connector would let a *partial* port win the name collision — first directory wins, **with no
error** — and take those six tools and every durable job off the agent's surface. The manifest here
is this repository's own declaration of the served surface, checked against the running server by
`tests/test_server.py`; it is not an instruction to point Chemclaw3 at it.

**Two things follow from that, and the second is the answer to "isn't twenty tools a lot?".**
Because this server is never on the agent's surface, its tool count costs no prompt: the six
structure-in primitives are addressed by Chemclaw3's activities and would only ever reach a model
through the wiring the paragraph above already forbids. That is one more consequence of an existing
rule rather than a caveat of its own — and it is why the primitives live here rather than in a
second server, which would buy nothing and cost an image, a port and a release cadence.

(Were it ever wired up anyway, `endpoint.tools` is an allowlist that `connectors/registry.py` passes
to the client as `allowed_tools`, so a surface can be narrowed per tool. Worth knowing; not what
makes the count free here.)

## `calculation_key`: why the cache seam needed a tool of its own

Chemclaw3 calls this server from inside `science/calc/store.py::cached_compute`:

```python
hit = await store.get(key)  # <- the key is an argument, so it is needed BEFORE the compute
if hit is not None:
    return hit.result, True
result = await compute()
```

Returning `calc_version`/`calc_key` **on the result** is necessary and not sufficient: on a cache hit
there is no result to read them off. The only other way to fill that argument would be for Chemclaw3
to derive the key locally — which is exactly the silent divergence the split exists to remove.

So the server answers the same question one round trip earlier:

```python
identity = await remote.calculation_key(tool, arguments)   # cheap: canonicalise, embed, hash
hit      = await store.get(identity.key)                   # the four fields, ready to use
if hit is None:
    payload = await remote.<tool>(**arguments)             # the SCF, only on a miss
    await store.put(StoredResult(key=identity.key, result=payload))
```

One cheap round trip on a hit instead of a calculation; two calls on a miss, which is noise beside
minutes of CPU. `identity.key` is returned as the four fields `store.get` takes rather than a string
to parse — `calc_version` legitimately contains both `@` and `:` (`esol-delaney@2004/…`,
`cal-0.28733:-29.3116`), so a caller splitting the flat form is one delimiter from a key that misses
forever. The flat `calc_key` comes back beside it, and the compute result carries the same string, so
asserting the two against each other is a free check that both paths agree.
`tests/test_calculation_key.py` asserts exactly that property for every tool — the ten SMILES-in
and the primitives alike — which is what the whole design rests on. "Cheap" is asserted too: every route through `Calculator` is made to raise and all
every identity still comes back.

**One tool returns no key, and says why in a `caveat` rather than by omission.** `predict_logd`
never had one — Chemclaw3 did not cache logD, because its expensive half is already a cached pKa and
Crippen LogP is sub-millisecond — and the caveat names the pKa whose key *is* available.

**The two CREST searches key like everything else and refuse like nothing else.**
`CrestSpec.calc_version()` answers `crest-absent` when the binary is missing rather than raising, so
a key *is* derivable with no crest — and it would be a well-formed identity naming a program that
cannot run, addressing a row nothing will ever write. That is the same shape as the
`binary_version()` trap this port exists to contain, so the probe refuses exactly where the search
would.

`compute_thermochemistry` was briefly a second no-key tool, and resolving that is what produced the
primitive set. See "Why `compute_thermochemistry` is not here".

## What every result carries that Chemclaw3's did not

**`calc_version`, always — and `calc_key`, the full
`calc_type@calc_version:input_hash:params_hash` string, on all but `predict_logd`.**

`calc_version` is assembled from things that live only in this process:

- the installed `tblite` and `rdkit` **distribution versions** (a new RDKit shifts the seeded ETKDG
  embedding, so every energy computed on it moves);
- `_HAMILTONIAN_REVISION`, a constant in `engine/xtb_engine.py`;
- `xtb --version`, a subprocess, wherever the backend resolves to the binary;
- and, for pKa, **seven** calibration settings (both slopes, both intercepts, both uncertainties and
  the solvent).

On the Chemclaw3 side that string is half of the calculation cache's key **and the primary key of
the calibration ledger** — `predictions` is unique on `(calc_type, calc_version, input_hash)` and is
read with an *exact* match, no version pooling, deliberately, so a v1 that ran high is never averaged
with a v2 that ran low. After the split a Chemclaw3 pod has neither distribution installed and no
xtb binary.

The failure mode if a client rebuilt the string itself is the one worth writing down:
`xtb_cli.binary_version()` returns the literal string `"absent"` when the binary is missing **rather
than raising**. So the reconstruction would be well-formed, would match zero ledger rows, and
`calculator_trust("pka")` would confidently report `UNCALIBRATED`, n = 0 — a statement about a
calibration that is merely unreachable. Silent, not loud, which is why it is a returned field and a
test (`tests/test_calc_version.py`) rather than a convention.

### The three copied definitions — and the one that still has to agree

Neither repository may import the other, so three definitions are copied. Each has a test pinning it
against literal strings taken from Chemclaw3:

| Copy | Chemclaw3's original | Pinned by |
| --- | --- | --- |
| `engine/ids.py` — `stable_hash` | `chemclaw/core/ids.py` | `tests/test_key_contract.py` |
| `engine/key.py` — `CalculationKey`, `CALCULATION_EPOCH` | `chemclaw/science/calc/store.py` | `tests/test_key_contract.py` |
| `engine/chem.py` — `require_canonical_smiles` | `chemclaw/core/chem.py` | `tests/test_canonicalization_contract.py` |

**None of them has to *agree* across the two repositories any more, and that is what
`calculation_key` bought.** Because Chemclaw3 never derives a key — it receives the four fields
`store.get` takes — the copies here are the sole producer of every key this server's answers are
addressed by, and self-consistency is enough. One constant is worth a paragraph anyway, because it
is the one people expect to be an exception:

- **`CALCULATION_EPOCH`.** A source constant in both repositories, folded into every `params_hash`,
  bumped when a ChemClaw-side change makes an already-written row wrong.

  **The two compose; they are not compared, and the older claim that they "must match" rested on a
  premise that has since gone.** That premise was that Chemclaw3 still builds keys for its own
  in-tree calculators. It does not: `CalculationKey.build` has **no caller left** in its `src/`, and
  `cached_compute` has exactly one — `connectors/calc/remote.py::cached_remote`. Every row in
  `calculation_results` is now keyed by `remote_key`, which rebuilds this server's four fields and
  folds *its* epoch over **our** `params_hash`:
  `stable_hash({"epoch": <theirs>, "remote_params": <ours>})`. So a bump on either side alone
  changes the composed digest and misses every stored row, which is exactly what an epoch is for.

  Move them together anyway — it keeps the two epoch logs describing the same events — but as a
  convention, not as a correctness invariant, and knowing that a unilateral bump costs CPU rather
  than serving a stale row. `servers/calc/tests/test_key_contract.py` pins what a divergence would
  actually break: the pure `stable_hash`, the `{"epoch": ..., "params": ...}` envelope, the flat
  string format, and the four field *names* `remote_key` reads.

Three things that would otherwise be on that list are **not**:

- *The calculator settings.* `engine/config.py` uses Chemclaw3's env prefix and field names —
  `CHEMCLAW_PKA_UNCERTAINTY`, `CHEMCLAW_XTB_METHOD`, `CHEMCLAW_XTB_ENGINE` — because an operator
  configuring both should not need a second vocabulary. Seven of those values are *inside* the pKa
  version string, but only this server reads them, so tuning a calibration here moves the version
  everywhere it appears, consistently.
- *The RDKit build.* `structure_id` is a hash of an embedded geometry, and only this server embeds.
  (The derivation itself must stay put: round the coordinates to `xtb_geometry_decimals` first, then
  `stable_hash` over `{elements, positions, charge, multiplicity}`, excluding `smiles` and `origin`.
  It is the `input_hash` of every `xtb.*` key.)
- *The flat key format.* `calculation_key` returns the four fields, so nothing parses the string.

## The `xtb` and `crest` binaries: both ship, in their own build stage

**A Python dependency cannot install either**, since both are compiled Fortran distributed through
conda-forge rather than PyPI — so the `Containerfile` installs them the way they are actually
distributed, with `micromamba` into a self-contained prefix the final image copies and puts on
`PATH`. Versions are pinned (`CREST_VERSION`, `XTB_VERSION`) because both are interpolated into
`calc_version`: an unpinned rebuild would silently re-key every cached calculation.

**Why this changed.** For `xtb` the absence was a speed limit, as the rest of this section explains.
For `crest` it was the capability: there is no in-process substitute, so the four sampling
primitives refused every call — and, being unreachable, three of the four had shipped broken.
Driven against crest 3.0.2 for the first time, `--deprotonate` raised a validation error on every
molecule, `--protonate` looked for a filename no CREST version writes, and both would have handed
back a charged species carrying the neutral's charge. See
`D-2026-08-26-a-sampler-nobody-ships-is-a-refusal-with-a-manual` in Chemclaw3's `docs/decisions/`.

**Licence.** `crest` is GPL-3.0 and `xtb` LGPL-3.0. Both are invoked as separate processes over
files and neither is linked into this codebase, so the licences do not reach this source — but
shipping them in an image is distribution, and its obligations (offer of source, licence texts)
attach to whoever publishes the image. That was decided deliberately; it is not an accident of a
base image.

**The `xtb` binary is installed and is deliberately not the default backend.** The image pins
`CHEMCLAW_XTB_ENGINE=tblite`, because `auto` resolves to the binary the moment one is on `PATH` and
the backend is part of `calc_version` — measured, `opt-GFN2-xTB+tblite-0.7.0/…` becomes
`opt-GFN2-xTB+xtb-6.7.1/tblite-0.7.0/…`. Three consequences follow from that one difference and none
of them announces itself: every row in `calculation_results` misses forever, every reconciled
residual in Chemclaw3's calibration ledger becomes unreachable (so `calculator_trust("pka")` reports
a confident `UNCALIBRATED`, n=0), and `predict_pka`'s base branch — which relaxes both species
through this backend — computes numbers its calibration was not fitted on, since the shipped slope
and intercept came from the tblite path. So the binary ships *available* rather than *active*:
`CHEMCLAW_XTB_ENGINE=xtb` or `auto` turns it on for a deployment that wants ANCopt and GFN-FF and
has decided to recompute. CREST is unaffected either way, because it carries its own GFN
implementation.

**Removing them is supported and is not silent**: `crest_cli.is_available()` goes False, the
sampling primitives refuse by name, and `XtbSpec.resolve_backend()` falls back to `tblite` with the
version string saying so.

### What `xtb` buys over the in-process path

`tblite` — which *does* ship manylinux wheels — carries the same GFN1/GFN2 Hamiltonians in-process,
which is why the `xtb` binary is an optimisation rather than a capability. Everything the xTB tools do
runs on it: single points, properties, Fukui indices, the L-BFGS-B optimizer over tblite's analytic
gradient, the finite-difference Hessian, and both pKa branches. Chemclaw3's own deployment resolves
to `tblite` too, for the same reason, so **the numbers and the `calc_version` strings this server
produces are identical to the ones it produced before the split** — verified by deriving the same
keys and the same energies from both trees on the same package versions.

What is given up without the binary:

- **ANCopt**, xtb's approximate-normal-coordinate optimizer. Measured ~7x on a 76-atom substrate and
  ~9x on 118 atoms, optimization plus Hessian. The in-process path is preconditioned
  (`engine/anc.py`, ~2x over plain Cartesian L-BFGS) but does not close that gap; most of what
  remains is the Hessian, which xtb computes analytically and this does not.
- **GFN-FF**, xtb's force field. `optimize_geometry` with `method="GFN-FF"` raises a message naming
  the missing binary rather than substituting GFN2.

`engine/xtb_cli.py` is ported in full, and the `auto` default (`CHEMCLAW_XTB_ENGINE`) finds the
binary on `PATH`, routes to it, and —
the part that matters — **moves every `calc_version` to say so**. Two pods, one with the binary and
one without, therefore compute different keys for the same molecule. That is wanted rather than
tolerated: the two backends do not agree to the last decimal, and a shared key would serve one
program's number as the other's. Pin `CHEMCLAW_XTB_ENGINE=tblite` to remove the split.

Open-shell species always route in-process regardless, because the `xtb` 6.6.1 binary's `--spinpol`
is OOM-killed in that build and GFN2 without a spin-polarization term does not stabilize an open
shell at all — measured, it put triplet O₂ *above* singlet.

## Cost: this server is allowed to be slow, and not allowed to be stateful

`props` answers from a dict; a CREST search here can run for hours. **Duration is not the property
this fleet promises** — statelessness is, and `docs/adding-a-server.md` now says so rather than
naming a number.

What that buys and what it costs:

- `request_timeout: 900` in the manifest, against the fleet's habitual 30, because a cold geometry
  optimisation on a drug-sized molecule is measured at 38 s (ibuprofen, 33 atoms, 24 steps). It is
  the *ordinary* tier: the two long ones are dialled with their own client budgets and are stated
  below, and it is this number the pod's `terminationGracePeriodSeconds` is derived from, because a
  rollout that waited out a four-hour CREST search would not be a rollout. A budget that states the
  real cost beats one that kills a calculation three quarters of the way through.
- **Two bounds on the input and one on the clock**, because the Hessian's cap was not the only
  quadratic cost and `request_timeout` bounds the caller's wait rather than the work:
  - `CHEMCLAW_XTB_HESSIAN_MAX_ATOMS` (150) bounds the primitive whose cost is 6N single points.
  - `CHEMCLAW_XTB_MAX_ATOMS` (500) bounds **every** structure, on `Structure` itself so each
    primitive inherits it. The optimizer is the other quadratic one: its ANC preconditioner builds a
    dense (3N, 3N) model Hessian and eigendecomposes it *once per leg* — measured here at 3.6 s for
    120 atoms, 11.6 s for 240 and 32.9 s for 510, and at the ~42,000 atoms a body under the 1 MB cap
    can carry, a 127 GB allocation that takes the process down with every other connected turn.
  - `CHEMCLAW_XTB_INLINE_TIMEOUT_SECONDS` (780) bounds the in-process optimisation and Hessian
    loops, checked per gradient and per displacement. The two subprocess timeouts do not cover
    those paths, and they are the paths this image runs; cancelling the awaiting coroutine does not
    stop the worker thread, so without this a caller that gave up left the CPU burning and its
    retry started a second burn beside the first.
- **Every budget here is its caller's bound less 120 s, and all three used to be *equal* to it.**
  Chemclaw3 waits `calc_server_timeout_seconds` (900), `calc_atomic_timeout_seconds` (3600) and
  `calc_sampling_timeout_seconds` (14400) for the three tiers of this server, and this server
  allowed exactly 900, 3600 and 14400 back. Equal is not tighter: the caller's clock starts when it
  sends the request and this server's starts after connect, handshake, JSON decode, structure
  embedding and admission, so the caller always expired first — and every deliberately worded
  refusal here ("run a smaller system, relax it first, or raise
  CHEMCLAW_XTB_INLINE_TIMEOUT_SECONDS") was unreachable in production, with the pod left computing
  for a request nobody was waiting for. The margin is 120 s because `budget.Deadline` is checked
  *between* single points and never inside one, and one single point at the 500-atom ceiling is
  **81 s** measured here (53 atoms 0.20 s, 153 atoms 2.43 s, 303 atoms 19.8 s, 453 atoms 62.7 s,
  493 atoms 81.1 s). It costs each tier 120 s of affordable calculation — 13% of the inline budget,
  under 1% of the sampling one. `tests/test_cost_bounds.py` holds the ordering rather than the
  numbers, because the numbers are a deployment's to change and the ordering is not.
- **Nothing is cached in this process**, deliberately. That is what `calculation_key` is for: the
  caller checks its own store and only reaches a compute tool on a miss.
- Every tool body runs its work in a worker thread, and `tests/test_event_loop_offload.py` asserts
  the hop for every one that dispatches one — deriving that set from the served surface rather than
  from a count written here, which is why no count is written here. One call on the event loop would
  stop every other connected turn on this process for its whole duration — which could be an hour.

**What would make something belong on the other side of the seam**: wanting to persist anything.
A job record, a resumable checkpoint, a progress channel, a partial-result cache. None exists here,
and three things that would have needed one were left in Chemclaw3 rather than built —
`progress.py`'s callback, the thermochemistry refinement loop, and the sweep half of a relaxed scan.

The image pins `OMP_NUM_THREADS=1` (and the BLAS equivalents). LAPACK and tblite's OpenMP each size
themselves to the *node* rather than to the container's CPU limit and then fight each other;
concurrency belongs at the request level, where the server can see it. Raise it deliberately, on a
pod sized for it.

**And it is now bounded there.** One in-process calculation is one core under that pinning, so
aggregate load was whatever the callers happened to send: a burst thrashes the pod, every call takes
longer than it would have alone, the caller's timeout fires, and its retry lands beside a
calculation that never stopped. `CHEMCLAW_CALC_MAX_CONCURRENT_REQUESTS` (default 4) is the ceiling,
and a call arriving at a full pod is **refused promptly rather than queued** — a queued minute-long
calculation comes back long after `request_timeout`, computed at the expense of one somebody is
waiting for.

**The ceiling is a budget of cores, not a count of calls**, and CREST is why. The
`OMP_NUM_THREADS=1` pin binds this process; it is scrubbed out of the sampler's environment, which
is then given `-T`/`OMP_NUM_THREADS` from `CHEMCLAW_CREST_THREADS` (4 in this image). So an
in-process calculation costs one slot and a CREST search costs `crest_threads` — counting calls
admitted four searches, i.e. sixteen runnable threads on a four-core pod, measured at 4.2x the
wall clock of one search alone. A search on a pod configured smaller than that runs alone rather
than being refused forever.

The tools outside the gate are the manifest's `read_only` ones (`calculation_key`,
`embed_structure`, `combine_structures`), and the split is that classification rather than a cost
judgement: `calculation_key` is how a client avoids work, so refusing it under load adds work.
Cheapness is *not* the rule — `predict_solubility` and `predict_developability_profile` run no SCF
either, cost a few milliseconds, and are gated, because both manifests classify them
`state_changing`. Chemclaw3 declares the same number as `calc_backend_max_concurrent_requests`, which counts
*requests* rather than slots and so agrees with this one only while every request costs one; this is
the backstop for every other caller and for exactly that disagreement.
See `engine/admission.py`.

## The Hessian on the wire

`compute_hessian` returns matrices, and they are the only payload here big enough to need a decision.

**Format: base64-encoded `.npy`.** Three reasons, in order: it round-trips float64 **exactly**
where a JSON array of decimal literals does not (and a format that lost the last few bits would put
a silent error into every frequency derived from it); it is self-describing about shape and dtype,
so a truncated payload fails to load rather than reshaping into something plausible; and it is
byte-for-byte what Chemclaw3's `calculation_artifacts` table already stores, so a caller can put the
bytes straight into its artifact store without a second serialization to disagree about.

**Ceiling: ~2.2 MB, bounded by the atom cap.** At the default `CHEMCLAW_XTB_HESSIAN_MAX_ATOMS` of
150 the matrix is 450×450 float64 — 1.62 MB raw, 2.16 MB base64 (measured, not estimated) — and the
dipole derivatives add 450×3 = 10.8 kB. That is above `mcp_server_kit.DEFAULT_MAX_REQUEST_BYTES`
(1 MB), which caps *requests* and so does not apply to a response — but it is the number to check a
proxy against. The ceiling falls quadratically with the cap; `tests/test_engine.py` asserts both.

Exactly one of `dipole_derivatives_npy` and `ir_intensities` is populated, and which one says which
backend ran: the in-process path collects dipole derivatives as it displaces, the `xtb` binary
computes intensities itself. Both are what a caller needs to derive an IR spectrum; neither is a
spectrum, because the normal-mode projection and the RRHO arithmetic stayed in Chemclaw3.

**`max_gradient_hartree_per_angstrom` travels beside them**, and it is the only thing in the payload
that says whether the eigenvalues mean frequencies. This primitive differentiates whatever geometry
it is handed — a transition state and a scan point are legitimate subjects, so it does not refuse a
non-stationary one — and the number costs nothing, because the undisplaced single point computes the
analytic gradient anyway and used to discard it. It matters because the failure it exposes is
silent: Chemclaw3's `thermo._vibrational` drops every mode with `wavenumber <= 0`, so a Hessian on an
unrelaxed embedding yields a ZPE, a thermal correction and an entropy that all look ordinary, and a
geometry displaced along a soft, positively curved direction shows no imaginary mode for
`is_minimum` to catch. `None` on the binary backend, which reports no gradient beside its Hessian.

## What was left behind, and why

| Left in Chemclaw3 | Reason |
| --- | --- |
| `store.py`, `postgres_*.py`, `artifacts.py`, `calibration.py`, every `run_cached_*` | The cache, the artifact store and the calibration ledger. Only `CalculationKey` + `CALCULATION_EPOCH` came across, as `engine/key.py`. |
| `xtb_thermo.py` and the `compute_thermochemistry` tool | A composite whose key names its own output. See "Why `compute_thermochemistry` is not here". |
| `reaction.py` | Pure composition over primitives already exposed — per-species optimise + Hessian, stoichiometric sums, a formula-balance check. |
| `xtb_scan.run_scan`'s sweep | A loop. The point is here; the profile arithmetic and the point cap are the caller's. |
| `conformers.py` / `complexes.py` arithmetic | Boltzmann populations, conformational entropy, the interaction-energy subtraction. All arithmetic over what the primitives return. |
| `progress.py` | A progress callback needs somewhere to report to, and that is job state. |
| `specs.py`, `activities.py`, `workflows.py`, `worker.py`, `results.py` | The Temporal half of the bundle. |
| `authz.expensive_actions` (`expensive: true` on the ensemble jobs) | An authorization decision about a person. A tool server has no basis to make one, and this one does not try. |
| `geometry.py` | A cross-method "good geometry" pointer, written into the store on every optimisation miss. |
| `uncertainty`'s ledger-facing half | `Estimate`, `structural_domain` and `CalculationDomainError` are here because `SolubilityResult.estimate` and every domain refusal are made of them; nothing that reads residuals is. |
| the bundle's `skills:` key | A skill is architecture layer 3 in Chemclaw3 and this fleet has no equivalent seam. |

Things that were **added** rather than ported, each because the seam needed them:

- `calculation_key` — `cached_compute` takes the key as an argument, so a key that only arrives on
  the result cannot serve a lookup.
- `embed_structure` and `combine_structures` — a caller cannot build a `Structure` itself without
  re-deriving `structure_id`, which is the divergence this whole design removes.
- `Structure.structure_id` became a `computed_field`. As a plain property it did not serialize at
  all, so a geometry crossing the wire arrived without its content address. Caught by the wire test,
  not by any unit test.
- `XtbSpec` refuses a solvent ALPB has no parameters for, at construction. Chemclaw3 catches that in
  a durable-job precondition, which does not exist here.

And two behaviours moved *into* the calculators when their cached wrappers were deleted, so the
uncached entry points cannot be taken wrongly: `predict_pka` and `compute_descriptor_profile`
canonicalise their own SMILES.

## Running it

```sh
make run-calc                      # 127.0.0.1:8860, token defaults to `dev-token`
uv run pytest servers/calc -q      # ~17 s; every tool is exercised on a real SCF
```

`engine/` <- `tools.py` <- `app.py`, one-way. `tests/test_engine.py` imports no transport;
`tests/test_server.py` runs the real app under uvicorn and talks MCP to it.
