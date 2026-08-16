# `calc` — GFN2-xTB, pKa, solubility, logD and developability descriptors · port 8860

Nine request/response calculators, ported from Chemclaw3's own in-tree `calc` connector. No cache,
no artifact store, no calibration ledger, no durable jobs, and no network call at any point: this
server takes a SMILES, computes, and returns.

| Tool | What it computes |
| --- | --- |
| `compute_xtb_energy` | GFN2-xTB single-point total energy (Hartree). |
| `compute_electronic_properties` | HOMO/LUMO/gap (eV), dipole (Debye), Mulliken charges, Wiberg bond orders. |
| `predict_site_reactivity` | Condensed Fukui indices, atoms ranked by susceptibility to attack. |
| `optimize_geometry` | Relaxation to a stationary point of the GFN2 surface. |
| `compute_thermochemistry` | Frequencies, IR spectrum, and ideal-gas RRHO ZPE/H/S/G. |
| `predict_pka` | pKa of the most acidic O-H/S-H site, or a base's conjugate acid (pKaH). |
| `predict_solubility` | Aqueous log S from the ESOL (Delaney 2004) baseline, with a domain check. |
| `predict_logd` | pH-dependent logD, for singly-ionisable molecules only. |
| `predict_developability_profile` | MW, cLogP, TPSA, H-bond counts, sp3 fraction, QED, Ro5/Veber flags. |
| `calculation_key` | What a calculation *would* be stored under — without running it. |

## Read this first: it is a backend, not a connector Chemclaw3 dials

`chem` and `safety` are complete ports, so registering them on `CHEMCLAW_CONNECTORS_DIR` swaps one
implementation for an identical one. **This server is different, and putting it on that path is
wrong.**

Chemclaw3 keeps its `calc` bundle and all fifteen of its tools. What moved here is the *computation*
behind nine of them; the other six have no computation to move, because they **are** the state:

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

## `calculation_key`: why the cache seam needed a tenth tool

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
`tests/test_calculation_key.py` asserts exactly that property for every tool, which is what the whole
design rests on. "Cheap" is asserted too: every route through `Calculator` is made to raise and all
nine identities still come back.

**Two tools return no key, and say why in a `caveat` rather than by omission.** `predict_logd` never
had one — Chemclaw3 did not cache logD, because its expensive half is already a cached pKa and
Crippen LogP is sub-millisecond. `compute_thermochemistry`'s key names the geometry its refinement
loop *finally settled on*, which is an output: the loop optimises, takes a Hessian, and displaces
along the imaginary mode and repeats when the optimiser lands on a rotational saddle (which an
ordinary ester does). Chemclaw3's own `compute_thermochemistry` was not a single cached calculation
either — its economy came from the nested `xtb.opt` and `xtb.hess` entries, and the split has moved
those inside one remote call. The optimisation's key is deliberately *not* offered as a stand-in: it
would usually miss, and where Chemclaw3 happened to hold an `xtb.hess` row for that unrefined
geometry it would be a hit on the wrong answer.

## What every result carries that Chemclaw3's did not

**`calc_version`, always — and `calc_key`, the full
`calc_type@calc_version:input_hash:params_hash` string, on eight of the nine.**

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

**But only one of them actually has to *agree* across the two repositories, and that is what
`calculation_key` bought.** Because Chemclaw3 never derives a key — it receives the four fields
`store.get` takes — the copies here are the sole producer of every key this server's answers are
addressed by. Self-consistency is enough for three of the four; the exception is:

- **`CALCULATION_EPOCH`.** A source constant in both repositories, folded into every `params_hash`,
  bumped when a ChemClaw-side change makes an already-written row wrong. Chemclaw3 still builds keys
  for its own in-tree calculators, so the two live in one table and must match. Bump it in both in
  the same change, or in neither.

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

## The `xtb` and `crest` binaries: not in this image

**They are not installed, and a Python dependency cannot install them.** Both are compiled Fortran
programs distributed through conda-forge and distribution packages rather than PyPI, so
`pyproject.toml` has no way to express them and the `python:3.11-slim` base carries neither.

This is a speed limit, not a capability gap, and the reason is that `tblite` — which *does* ship
manylinux wheels — carries the same GFN1/GFN2 Hamiltonians in-process. Everything the nine tools do
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

`engine/xtb_cli.py` is nonetheless ported in full, and turning it on is one layer plus nothing else:
install `xtb` on `PATH` and the `auto` default (`CHEMCLAW_XTB_ENGINE`) finds it, routes to it, and —
the part that matters — **moves every `calc_version` to say so**. Two pods, one with the binary and
one without, therefore compute different keys for the same molecule. That is wanted rather than
tolerated: the two backends do not agree to the last decimal, and a shared key would serve one
program's number as the other's. Pin `CHEMCLAW_XTB_ENGINE=tblite` to remove the split.

Open-shell species always route in-process regardless, because the `xtb` 6.6.1 binary's `--spinpol`
is OOM-killed in that build and GFN2 without a spin-polarization term does not stabilize an open
shell at all — measured, it put triplet O₂ *above* singlet.

## Cost, and why the timeout is 900 s

This is the only server in the fleet where a single call can take minutes. `props` answers from a
dict; `chem` draws an SVG; a cold `compute_thermochemistry` here is 6N + 1 GFN2 single points with a
geometry optimization in front of it.

**It therefore breaks the fleet's "anything over ~20 s is a durable job" rule, on purpose, and the
exception is bounded rather than argued away.** Three things hold; a slow tool with none of them is
still a durable job:

1. a **hard input bound** refuses what would run away — `CHEMCLAW_XTB_HESSIAN_MAX_ATOMS` (150), with
   a message naming Chemclaw3's durable QM job path, which is the route that *does* exist for those
   molecules;
2. the manifest states the real budget (`request_timeout: 900`) instead of inheriting the fleet's
   habitual 30 s and timing out mid-calculation;
3. the tool docstring tells the model what it is asking for, so an expensive call is a decision
   rather than a routine follow-up to an energy.

**There is no cache in this process**, and that is what `calculation_key` is for: the caller checks
its own store first and only reaches a compute tool on a miss. `compute_thermochemistry` is the one
tool that cannot be looked up that way, so it is also the one whose cost is paid every time — which
is exactly why its input bound is the tightest thing on this list.

Every tool body runs its work in a worker thread, and `tests/test_event_loop_offload.py` asserts the
hop for all ten — `calculation_key` included, because it embeds a 3D geometry and, if a client uses
it before every compute, it is the most frequently called tool here. One call on the event loop
would stop every other connected turn on this process for its whole duration.

The image pins `OMP_NUM_THREADS=1` (and the BLAS equivalents). LAPACK and tblite's OpenMP each size
themselves to the *node* rather than to the container's CPU limit and then fight each other;
concurrency belongs at the request level, where the server can see it. Raise it deliberately, on a
pod sized for it.

## What was dropped in the port, and why

| Dropped | Reason |
| --- | --- |
| `store.py`, `postgres_store.py`, `postgres_artifacts.py`, `artifacts.py` | The calculation cache and the artifact store. Only `CalculationKey` + `CALCULATION_EPOCH` came across, as `engine/key.py`. |
| `calibration.py` | The prediction/measurement ledger. It is a Postgres table and its six tools stay in Chemclaw3. |
| every `run_cached_*` wrapper | Each became its uncached body plus a key in the result. |
| `xtb_hessian`'s persistence half | `HessianResult`, `_pack`/`_unpack`, `_load`, `_persist`, `run_cached_hessian` — all store operations. `compute_hessian` returns the matrix instead of `(matrix, blobs_to_store)`. |
| `xtb_cli`'s artifact capture | It read the run's `hessian`/`vibspectrum` files out of the tempdir for a blob store that does not exist here. |
| `geometry.py` | A cross-method "good geometry" pointer, written into the store on every optimization miss. |
| `crest_cli.py`, `XtbSpec`'s `CrestSpec` | CREST runs the conformer-ensemble and host–guest tasks, which are durable jobs and stayed behind. Keeping the spec would put `crest --version` into a version string for a program that never runs — the exact thing `calc_version`'s own rule forbids. |
| `complexes.py`, `conformers.py`, `reaction.py`, `xtb_scan.py`, `specs.py`, `results.py`, `activities.py`, `workflows.py`, `worker.py` | The durable-job half of the bundle. |
| `solvents.require_supported_solvents` | A durable-job *precondition*. The table and the message came across; the check moved into `XtbSpec`'s validator, which is what puts it in front of both backends. |
| `uncertainty`'s ledger-facing half | `Estimate`, `structural_domain` and `CalculationDomainError` are here because `SolubilityResult.estimate` and every domain refusal are made of them; nothing that reads residuals is. |
| the bundle's `skills:` key | A skill is architecture layer 3 in Chemclaw3 and this fleet has no equivalent seam. |

Two behaviours moved *into* the calculators when their cached wrappers were deleted, so the uncached
entry points cannot be taken wrongly: `predict_pka` canonicalizes its SMILES itself (atom order
steers the seeded embedding, so computing on the caller's spelling would make the value depend on
which spelling arrived first), and `compute_descriptor_profile` does the same.

Two things were **added** rather than ported. `calculation_key` is the larger one, and it exists
because `cached_compute` takes the key as an argument — see above. The smaller: `XtbSpec` refuses a
solvent ALPB has no parameters for, at construction.
Chemclaw3 caught that in a job precondition before a workflow started; without it here, "2-MeTHF" —
among the commonest process solvents, and one GFN2-xTB has no parameters for — would surface as
tblite's "String value for epsilon was not found among database of solvents", or minutes later
inside a subprocess.

## Running it

```sh
make run-calc                      # 127.0.0.1:8860, token defaults to `dev-token`
uv run pytest servers/calc -q      # ~12 s; every tool is exercised on a real SCF
```

`engine/` <- `tools.py` <- `app.py`, one-way. `tests/test_engine.py` imports no transport;
`tests/test_server.py` runs the real app under uvicorn and talks MCP to it.
