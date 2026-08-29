# The module catalogue

Every MCP server this fleet has or plans, why it earns a place, and the port it owns. This file is
the authoritative port registry: claim the next free port here in the same pull request that adds
the server.

**Status** is one of `built`, `next` (agreed, queued), or `proposed` (in the catalogue, not yet
argued through).

**A catalogue entry is a claim about what is available, and claims go stale.** Three rows were
re-derived against 2026 releases on 2026-08-25 — `nomenclature`, `admet` and `retro` — and each
says so inline. One of the three came back the *opposite* of what prompted the check, which is the
argument for doing it against sources rather than from memory. Re-derive on the same trigger the
consuming repository uses for its upstream register: whenever a dependency or a licence assumption
here is bumped. **Offline** says what the server reads, because production has no egress — see
`CLAUDE.md`, "No egress. Ever."

Nothing here duplicates a Chemclaw3 capability; the exclusion list is in `CLAUDE.md`.

---

## Tranche 1 — Route and process engineering

The tranche the fleet starts with: the questions a process chemist asks between a route on paper and
a batch in a plant, none of which Chemclaw3 can answer today.

### `props` — solvent and pure-component properties · port 8850 · **built**

Boiling and melting point, density, flash point, dielectric constant, Hansen parameters, water
miscibility, peroxide formation, ICH Q3C class and residual-solvent limit, and the GHS hazard flags
behind an indicative greenness band, for 44 process solvents. Computes vapour pressure at a
temperature, boiling point under vacuum, and a filtered, Hansen-ranked swap shortlist.

*Tools:* `list_solvents`, `solvent_properties`, `vapour_pressure`, `boiling_point_at_pressure`,
`solvent_swap_candidates`, `compare_solvent_properties` — all `read_only`.
*Offline:* a vendored, checksummed CSV compiled in this repository (CC0). No upstream at all.
*Why first:* pure, deterministic, genuinely offline by nature, and it exercises every part of the
mechanism the rest will copy. See `servers/props/README.md`.

### `rxnpredict` — forward reaction & condition prediction · port 8857 · **built**

What will this reaction give, and how would people run it? Several open-source predictors run in
parallel and their ranked outputs are combined by Borda-weighted voting, gated on a coarse SMARTS
reaction class. The per-model spread comes back with the consensus, because "four of five agree"
and "only the rule-based one produced this" are different answers.

*Tools:* `predict_forward_reaction`, `predict_reaction_conditions`, `predict_forward_single_model`,
`predict_conditions_single_model`, `list_available_models`, `classify_reaction` — all `read_only`.
*Offline:* model checkpoints baked in at build time (`scripts/fetch_models.py` in a builder stage),
with `HF_HUB_OFFLINE=1` at runtime; per-class trust priors as a checksummed vendored dataset.
*Provenance:* **a fork of [`chemclaw2_forward`](https://github.com/8fqycwdt8v-oss/chemclaw2_forward)**
(branch `claude/reaction-condition-meta-model-29YIz`, MIT, same owner) at commit `6affefb`. Upstream's
own tests for the aggregator, classifier, priors and preprocessing pass here unmodified, which is
the evidence the fork is the same model rather than a similar one.

**What was removed from upstream, so nobody restores it believing it was an oversight:**

- **The Phase-C `claude` predictor**, both adapters, `llm_prompts.py`, the `[claude]` extra and the
  `anthropic_*` settings. It called the Anthropic API per request and was live by default —
  `enabled_forward_models` defaults to `*`, so installing the extra with a key present was enough.
- **`clear_prediction_cache`**, with the diskcache it cleared. The cache is now an in-process
  bounded LRU, so there is no volume, no TTL, and no state-changing tool on this server at all.
- **`health_check` as a tool.** `/healthz` comes from the transport.

**What was fixed:** upstream applied its bearer check as a route `Depends(...)` while its MCP
surface was *mounted* — and a mount bypasses the enclosing app's dependencies, so the credential
guarded the REST routes and not `/mcp`. Here it is ASGI middleware, and `tests/test_server.py`
asserts the 401. See `servers/rxnpredict/README.md` for the full list.

### `rxnlabel` — reaction and species representations, roles, and named reactions · port 8865 · **built**

What is this reaction made of, and what is it called? Atom-maps a reaction and decides what every
species was doing — starting material, product, reagent, solvent, catalyst, **ligand**, **base**,
additive — then computes each species' canonical form, Bemis-Murcko scaffold and functional groups,
and classifies the reaction from 527 curated SMIRKS.

The last two roles are the point. The vocabulary an ELN column or a patent extractor uses has five
values and contains neither, so "which ligand was used" is unanswerable from a recorded reaction —
and it is most of what a chemist asks. Deciding them needs the *reaction*, not the molecule:
triphenylphosphine is a ligand in a Suzuki and a stoichiometric reagent in a Mitsunobu, and only the
rest of the flask distinguishes them.

*Tools:* `labeller_version`, `represent_reaction`, `name_reaction`, `represent_reactions`,
`name_reactions` — all `read_only`. The batch pair is what a corpus-labelling drain calls: a
multi-million-row corpus at one round trip per reaction is a multi-million round trips.
*Offline:* RXNMapper's checkpoint is pulled during the build, and the NetworkPolicy then denies
egress — so a failed bake degrades loudly instead of a pod reaching the internet from a cluster
that forbids it. Rxn-INSIGHT's SMIRKS ship in its wheel; the role rules and the functional-group
vocabulary are source in this repository.
*Optional by design:* both models are a `models` extra, because RXNMapper drags torch behind it and
a developer's checkout should not pay gigabytes to run a SMARTS test. Without them the server still
assigns roles and computes representations, and `labeller_version` records which components were
present — so rows labelled without one go stale the moment a deployment installs it, and the corpus
repairs itself.
*Not mounted on Chemclaw3's `CHEMCLAW_CONNECTORS_DIR`*, deliberately: these are internal primitives
for a background drain, and mounting the manifest would put them in the agent's prompt as tools to
choose between — the call `core/config/calculators.py` makes for the calculation server. Chemclaw3
addresses it through `rxnlabel_server_url` and `rxnlabel_server_token_env`. Registered in
`manifests-internal/`, not `manifests/`, and its manifest declares `mount: backend` — a key
Chemclaw3's `extra="forbid"` manifest model refuses, so mounting it is a startup error rather than
a silent widening of the agent's tool list.
*Port note:* 8865 rather than a tranche-1 slot, because 8850-8856 are all claimed by this
catalogue's own proposals and taking a proposed server's port silently is what
`test_the_catalogue_claims_no_port_a_server_contradicts` exists to prevent. It sits next to
`nomenclature` at 8864 for no reason other than that 8865 was the first free number.
It is the complement of the proposed `rxnsearch` (8855): that one *counts* precedent, this one is
what makes a corpus countable in the first place.

### `chem` — bench chemistry over RDKit · port 8858 · **built**

What do I weigh out, what is this compound, what does it look like, and how green is this route.
`resolve_compound` turns the name a chemist wrote (`DIPEA`, `Pd(dppf)Cl2`, `2-MeTHF`) into a
canonical structure — the bridge every structure-taking tool in the fleet needs;
`stoichiometry_table` scales a batch to the limiting reagent and converts solvent *volumes* into
real masses; `green_metrics` computes E-factor and PMI from exactly those masses; `render_structure`
draws a molecule or reaction as an inline SVG, optionally with chosen atoms highlighted; and
`enumerate_torsions` lists the bonds that can be rotated, each under a handle that survives a
rewritten SMILES — because an atom index does not, and a torsion scan driven from a stale one
returns a plausible barrier for a different bond with no error anywhere.

**The six enumerations are the free half of Chemclaw3's multi-step protocols.** `rank_species` and
`survey_bond_strengths` rank a *set* of structures; these produce the set from the molecular graph
at no cost, which is what lets Chemclaw3's skills state the rule as *enumerate, then compute, and
never the reverse*. `describe_topology` is the one to ask first — it says whether a search would
find anything, and the commonest waste in that catalogue is paying for a conformer search to
discover the molecule was rigid. Each enumeration bounds its output and **refuses past the bound
rather than truncating**: a partial set silently redefines the universe a downstream population is
normalized over.

*Tools:* `resolve_compound`, `stoichiometry_table`, `green_metrics`, `render_structure`,
`enumerate_torsions`, `describe_topology`, `enumerate_tautomers`, `enumerate_protonation_states`,
`enumerate_stereoisomers`, `enumerate_bond_cleavages`, `enumerate_degradants` — all `read_only`.
*Offline:* a vendored, checksummed CSV of 61 reagents under 87 spellings (CC0), plus RDKit. No
upstream at all, and deliberately so: an external resolver (PubChem, OPSIN) is a request-time
network call, which this repository does not permit and which the common case does not need.
*Provenance:* **a port of Chemclaw3's own in-tree `chem` connector** — the same manifest name, the
same argument names, the same model-facing docstrings, plus `enumerate_torsions`, which that bundle
never had and whose name Chemclaw3's manifest now declares too. It is a replacement for
that bundle rather than a second implementation, and the two cannot both answer:
`CHEMCLAW_CONNECTOR_URLS` is keyed by name, and `CHEMCLAW_CONNECTORS_DIR` resolves a name collision
by first directory (`connectors/registry.py::_bundle_dirs`). Moving it here takes RDKit out of the
chat service's image and gives the surface its own release cadence.

**What changed in the port, so nobody restores it believing it was an oversight:**

- **`auth: {mode: none}` became bearer** (`CHEMCLAW_CHEM_TOKEN`). The in-tree bundle was only ever
  dialled over loopback from the same pod; a server in another image is dialled across a network.
- **The reagent table became a vendored dataset.** It was a Python dict in Chemclaw3
  (`core/reagents.py`); here it is `data/records.csv` with a licence and a checksum, because that
  is what this repository requires of every corpus it answers from.
- **`settings.structure_render_size_px` became `CHEMCLAW_CHEM_RENDER_SIZE_PX`** (same default,
  320). One integer does not earn a pydantic-settings object.
- **The reaction-drawing path was hardened.** Measured against the installed RDKit rather than
  read off the source: `ReactionFromSmarts` *raises* where the ported code checked for `None`,
  `">>"` parses to an empty reaction, and `"°C>>CC=O"` parses as *methane* — all three now refuse.

**The one thing to know before touching it:** `engine/chem.py` duplicates Chemclaw3's
canonical-SMILES definition, which on the Chemclaw3 side is the calculation-cache key (D-011) with
26 importers. Chemclaw3 is the authority and nothing here derives a key;
`tests/test_canonicalization_contract.py` pins the two together as literal strings so a divergence
is caught rather than served. See `servers/chem/README.md`.

### `safety` — cited hazard, genotoxicity and ICH impurity tables · port 8859 · **built**

Three questions a chemist asks separately, kept separate, each answered from a committed table with
a citation on it. `screen_hazards` matches 11 structural motifs (azides, diazo, diazonium, peroxide,
nitrate ester, polynitroaromatic, perchlorate, hydrazine, N-halamine) plus 5 pairwise
incompatibilities checked across a reaction's components; `screen_genotoxic_alerts` matches 9
DNA-reactive structural alerts plus the nitrosamine formation route; `ich_impurity_limit` reads the
transcribed ICH Q3C residual-solvent limits and Q3D elemental-impurity PDEs.

*Tools:* `screen_hazards`, `screen_genotoxic_alerts`, `ich_impurity_limit` — all `read_only`, and
they must stay open under an unapproved plan: these are exactly the checks a chemist wants *before*
approving the work.
*Offline:* four vendored, checksummed YAML corpora plus RDKit, and a byte-identical copy of `chem`'s
reagent table (see below). No upstream at all — the obvious things to reach for here are an SDS
service, a hazard database or ICH's own site, and all three are request-time network calls.
*Provenance:* **a port of Chemclaw3's own in-tree `safety` connector** — the same manifest name, the
same three tools, the same argument names, the same model-facing docstrings, which are carried over
word for word because every disclaimer in them exists to prevent a mistake that was measured in a
live run. It is a replacement for that bundle rather than a second implementation, resolved the same
way `chem` is (name-keyed URLs, first directory wins a collision).

**Nothing here is a clearance, a classification or a risk assessment**, and each result says so in
its own payload as a pydantic `computed_field` rather than only in a docstring — a plain property is
dropped by serialization, which is how a caveat comes to exist in the code, pass every unit test, and
never reach the model writing the answer.

**Three things to know before touching it:**

- **`rules.yaml` is first-party (CC0-1.0), and its `dataset.json` records the basis, not just the
  identifier.** It shipped `UNRESOLVED` and is settled: every rule cites a source and reproduces
  none — Bretherick's and the four cited papers are prose containing no SMARTS, the patterns were
  written and debugged in Chemclaw3, and the underlying facts are not copyrightable. Same basis as
  `genotox`; deliberately *not* the basis `ich_q3c`/`ich_q3d` use, which transcribe guideline
  figures and carry ICH's terms. The test asserts the reasoning rather than the string, because a
  bare `CC0` typed to satisfy the loader is still the failure to prevent.
- **The deliberate omissions are load-bearing.** `tert`-butyl alcohol, water, `EDC`/`DMA`/`TCE`,
  and Ag/Au/Ni are absent on purpose, each because a value could not be verified against the source;
  and Q3C's "R9 / 2024" revision label is the one field nobody has checked, on every row's citation.
  A contributor "completing" the tables from memory reintroduces exactly the fabrication they
  replaced.
- **`data/reagents/` is a byte-identical second copy of `chem`'s corpus**, needed because a SMILES is
  not a spelling of a name and `ich_impurity_limit("C1CCOC1")` has to reach the tetrahydrofuran row.
  `tests/test_fleet.py` asserts the two files are equal from outside, which is what makes the
  duplication safe rather than merely accepted.

**What was dropped in the port:** Chemclaw3's `science/safety/notes.py` and the ~370 lines of tests
covering its `kg-validate` hazard gate (that gate reads knowledge-graph notes in a git repository,
which this fleet has none of), the `at_least` severity helper the gate was its only remaining caller
of, and the bundle's `skills:` key — the `safety-screening` SKILL.md stays in Chemclaw3, which is the
repository that has a skills layer. See `servers/safety/README.md`.

### `calc` — the physics behind Chemclaw3's calculators · port 8860 · **built**

Seventeen tools in three groups, none of them on any agent's surface — Chemclaw3 keeps its own
`calc` tools and calls this server from inside `cached_compute` and from Temporal activities.
**Eight** back its SMILES-in tools one for one: GFN2-xTB single-point
energy, electronic properties, condensed Fukui site reactivity, geometry optimisation, the xTB pKa
predictor, an ESOL solubility baseline with an applicability-domain check, pH-dependent logD and an
RDKit developability panel. **Seven** structure-in primitives Chemclaw3's durable-job activities
compose — `relax_structure`, `compute_properties_at`, `compute_fukui_at`, `compute_hessian`,
`scan_point`, `search_conformer_ensemble`, `search_binding_modes`. `compute_fukui_at` is the
geometry-taking twin of `predict_site_reactivity` and shares its `xtb.fukui` row: a flexible
molecule's site ranking is a property of the conformer, so averaging it over an ensemble means
asking per conformer, and only the caller holds those geometries. **Three** helpers that compute nothing:
`embed_structure`, `combine_structures` and `calculation_key`.

*Offline:* **no vendored dataset at all** — the first server in the fleet with none. Every number is
computed from tblite's compiled GFN parameters, RDKit's Crippen/QED tables and closed-form
arithmetic, all of which arrive inside their own wheels. `tests/test_no_egress.py` proves sufficiency
by running one of each kind of calculation with the guard armed, rather than by pointing at a corpus.
*Provenance:* **a port of the physics behind Chemclaw3's own in-tree `calc` connector** — its
request/response tools with their names, arguments and model-facing docstrings intact, plus the
compute half of its durable jobs re-cut as primitives.

**The seam is the thing to understand: Chemclaw3 keeps orchestration and the cache, this server
holds the physics.** It is *not* a connector Chemclaw3 dials — it is called from inside
`science/calc/store.py::cached_compute` as a backend on a miss, and registering it on
`CHEMCLAW_CONNECTORS_DIR` would let it win the `calc` name collision and take the calibration
ledger, the calculation cache, the artifact store and every durable job off the agent's surface,
with no error. That is now structural rather than stated: its manifest is registered in
`manifests-internal/`, which no published `export` line names, and declares `mount: backend`, which
Chemclaw3's manifest model refuses outright. See `docs/integration.md`.

**`cached_compute` takes the key as an *argument*, so a key that only arrives on the result cannot
serve the lookup.** Hence `calculation_key`, which answers what a calculation would be stored under
before running it — cheaply (canonicalise, embed, hash; no SCF, asserted by making every route
through `Calculator` raise) and as the four fields `store.get` takes rather than a string to parse.
Every result also carries `calc_version` and `calc_key`, and `tests/test_calculation_key.py` asserts
the two agree for every tool. Why it must be derived here: those strings come from the installed
`tblite`/`rdkit` versions, a Hamiltonian-revision constant, an `xtb --version` subprocess and seven
pKa calibration settings — none of which a Chemclaw3 pod has — and a local reconstruction would not
fail loudly, because `xtb_cli.binary_version()` answers `"absent"` rather than raising.

**This server may be slow and may not be stateful, and that replaced the fleet's ~20 s expectation.**
A CREST search runs for hours. What the fleet actually promises is that a tool takes its arguments,
computes and returns — no job record, no resumption, no progress channel. The structural test is
whether a calculation's key can be derived from its arguments: if it can, it is a primitive and
belongs here; if it names an output, it is a loop with state and stays in Chemclaw3.

**`compute_thermochemistry` is the worked example, and it is *not* on this server.** Its key names
the geometry its refinement loop settled on — an output — so `calculation_key` could not derive one,
and the measurement made the cost concrete: repeating it in Chemclaw3 costs 0.007 s against 0.816 s
cold for ethanol and 0.012 s against 3.273 s for ethyl acetate, two orders of magnitude coming
entirely from the nested `xtb.opt`/`xtb.hess` caches. Shipping it uncacheable would have converted
every repeat into a full recompute. Chemclaw3 assembles it instead from `relax_structure` +
`compute_hessian` + its own RRHO partition functions, and every part of that caches. The same
argument decomposed the scan (point, not sweep), the conformer ensemble (search, not populations)
and the interaction energy (search + three relaxations, not one composite); `reaction.py` contributed
no new primitive at all, being pure composition over ones already exposed.

**Four things to know before touching it:**

- **`xtb` and `crest` ship in the image**, installed from conda-forge in their own build stage
  because both are compiled Fortran and neither is on PyPI. They were absent for as long as this
  server existed, and the two costs were not comparable: `xtb` is an optimisation (ANCopt, ~7-9x on
  76-118 atoms, plus GFN-FF) over a `tblite` that carries the same Hamiltonians in-process, while
  `crest` has no in-process substitute — the four sampling primitives simply refused, and three of
  them turned out to be broken in ways no test could reach. Both versions are pinned, because both
  are interpolated into `calc_version` and an unpinned rebuild would re-key every cached row.
  `crest` is GPL-3.0 and `xtb` LGPL-3.0; both are run as subprocesses, never linked, and shipping
  them is a distribution decision recorded in the ADR. **`xtb` ships available rather than active**:
  the image pins `CHEMCLAW_XTB_ENGINE=tblite`, because the backend is part of `calc_version` and
  letting `auto` find the binary would re-key every cached row, orphan every calibration-ledger
  residual, and move the path `predict_pka`'s own calibration was fitted through — silently, on the
  day the image deployed.
- **The Hessian crosses the wire as base64 `.npy`**, which round-trips float64 exactly and is
  byte-for-byte what Chemclaw3's artifact store holds. The ceiling is ~2.2 MB, bounded quadratically
  by `CHEMCLAW_XTB_HESSIAN_MAX_ATOMS`.
- **Three definitions are copied from Chemclaw3**: `stable_hash`, `CalculationKey`/
  `CALCULATION_EPOCH`, and `require_canonical_smiles`. Because Chemclaw3 never derives a key,
  **nothing has to agree** across the two repositories. Not even the epoch: `CalculationKey.build`
  has no caller left in Chemclaw3's `src/`, and `connectors/calc/remote.py::remote_key` folds *its*
  epoch over **this server's** `params_hash`, so the two **compose** and a bump on either side alone
  invalidates every stored row. They move together by convention — it keeps the two epoch logs
  describing the same events — not because a divergence is silent. The calculator settings and the RDKit build do not, since only
  this server reads them and only this server embeds.
- **`Structure.structure_id` is a `computed_field`, not a property.** A plain property does not
  serialize, so a geometry crossing the wire arrived without its content address — and re-deriving
  one client-side is the divergence this design removes. Caught by the wire test, not a unit test.

## Platform — capability that is not a chemistry data source

One entry, and its own heading because it does not belong in a tranche. Every other server here
answers a chemistry question from a corpus. This one answers no question at all: it runs the
caller's arithmetic. It is grouped apart so the tranche structure keeps meaning what it says, and
its port is taken from the top of the block for the same reason.

### `pyexec` — a bounded, offline Python analysis sandbox · port 8899 · **built**

One tool, `run_python(code, data)`. Runs a short program in a disposable child process with numpy,
pandas, scipy, matplotlib, sympy, scikit-learn, RDKit and OpenBabel importable, and returns whatever
it assigned to `result`. For the work between tool calls — fitting a curve to points another tool
returned, aggregating a table, converting units, canonicalising a structure, checking a mass
balance, rendering a plot. Classified `read_only`, because nothing it writes outlives the call and
an analysis that cannot run until after a plan is approved is an analysis that cannot inform it.

*Offline:* **no vendored dataset, and no corpus to be sufficient.** What "offline" means here is
stronger and narrower than elsewhere in the fleet: the child holds no credential, cannot import a
network module, and cannot connect through a reference to one that a library already holds.
`tests/test_no_egress.py` proves the last of those by reaching for a connection from inside the
sandbox rather than by pointing at a table. `open()` is back in its builtins (2026-08-29), but every
path it resolves is jailed to that one call's own scratch directory, which is destroyed with the
rest of the call — so the offline claim is unaffected: nothing written there is a door to anywhere
else, and nothing written there survives the call that wrote it.

*Provenance:* first-party, and built to answer a question Chemclaw3 had left open. Its
`agent/scratchpad.py` withholds deepagents' `execute` verb, correctly — the two sandboxes that
framework ships are a third-party content egress and an unrestricted local shell. Neither refusal is
an argument against execution, and meanwhile numpy, pandas, scipy and RDKit sit installed in the
agent's own process with no way for it to reach them. This server is the way to reach them that does
not need the verb.

**The one thing to understand before editing it: half its controls are a boundary and half are
not.** The import guard, the guarded `open`, and the other withheld builtins are defence in depth
and porous by construction; the process, the hard rlimits, the built-not-filtered environment
together with the undumpable parent that keeps the *server's own* environment out of `/proc`, and
the empty `egress:` are what hold. `servers/pyexec/README.md` states which is which, and that
division is the design.

---

## Tranche 1, continued

### `thermalsafety` — runaway and thermal-hazard arithmetic · port 8851 · **next**

The calculations behind a safe scale-up, from calorimetry numbers the chemist supplies: adiabatic
temperature rise, MTSR, TMR_ad, SADT, the Stoessel criticality class, heat-removal capacity, and an
oxygen-balance screen for energetic functionality.

*Proposed tools:* `adiabatic_temperature_rise`, `mtsr`, `tmr_ad`, `sadt`,
`stoessel_criticality_class`, `heat_removal_capacity`, `oxygen_balance_screen`.
*Offline:* first-party formulas; no corpus needed.
*Note:* complements the `safety` server above, which screens *structures* for hazard alerts. This one
takes DSC/RC1/ARC numbers and answers "what happens if the cooling fails". The two belong apart:
`safety` answers from cited tables and needs RDKit, this one is arithmetic over numbers the chemist
supplies and needs nothing.

### `kinetics` — rate laws and reactor simulation · port 8852 · proposed

Fit a rate law to time-course data, simulate a batch/CSTR/PFR, extrapolate an Arrhenius fit, and
produce the heat-release profile `thermalsafety` consumes.

*Proposed tools:* `fit_rate_law`, `simulate_batch_reactor`, `simulate_cstr_pfr`,
`arrhenius_extrapolate`, `heat_release_profile`.
*Offline:* Cantera (BSD-3) + SciPy, both installed at build time.

### `unitops` — scale-up and unit-operation sizing · port 8853 · proposed

The correlations a process chemist reaches for when a route leaves the lab: mixing scale-up (P/V,
tip speed, N_js), heat-transfer time constants, Fenske–Underwood–Gilliland shortcut distillation,
crystallisation yield from a solubility curve, filtration and drying times.

*Offline:* first-party correlations.

### `retro` — retrosynthesis and route scoring · port 8854 · **adopted, not built**

A server already exists for this, written for chemclaw2, and it covers far more than this entry
ever proposed. Building a second would be the duplication this catalogue exists to prevent — see
"Adopted from the chemclaw2 fleet" below. What remains is integration work.

**Re-derived 2026-08-25, and the conclusion is unchanged.** 2026 brought reasoning-model
retrosynthesis — RetroReasoner, Retro-R1, and a line of work arguing that top-*k* accuracy is the
wrong metric for an LLM proposing routes. None of that changes this row: the entry was already
*adopted*, the existing server is the thing to integrate, and swapping its engine is a decision for
whoever owns it rather than a reason to build a second one here. What the 2026 work does change is
what "integrated" should eventually mean — a route needs a *strategy* rationale a chemist can argue
with, not a ranked list — and that is a note for the integration, not a new server.

### `rxnsearch` — reaction precedent statistics · port 8855 · proposed

*Proposed tools:* `conditions_for_transformation`, `yield_distribution`, `reagent_frequency`,
`precedent_count`.
*Offline:* an Open Reaction Database snapshot (CC-BY).
*Scope, deliberately narrow:* aggregate condition statistics only. Per-record ORD retrieval is
already Chemclaw3's `eln-ord` datasource plus `rxnfp` similarity, and a second read path over one
corpus is the duplication `CLAUDE.md` forbids.

### `blocks` — building-block sourcing and cost · port 8856 · proposed

*Proposed tools:* `search_building_blocks`, `price_and_lead_time`, `route_cost_rollup`.
*Offline:* a **mounted catalogue export** — a file the procurement team drops, read the way
Chemclaw3 reads ELN exports. Never a vendor API. `Chemclaw3_mock`'s vendor server stands in for it
in dev.

---

## Adopted from the chemclaw2 fleet

Two MCP servers already existed, written for chemclaw2. **`rxnpredict` has since been forked into
this repository** (tranche 1, where its entry is authoritative); `retro` stays where it is.

The two went different ways for a reason worth recording. `chemclaw2_forward` is a single process
whose heavy dependencies are already optional extras, so it forks cleanly and gains the fleet's
transport, auth and no-egress posture in the process. `chemclaw2_retrosynthesis` is 40+ engines
across as many containers with GPU profiles and its own release cadence; pulling that into this
workspace would buy nothing and cost its independence, and Chemclaw3's connector seam is built for
exactly that case (`D-2026-08-09-a-connector-we-do-not-run`).

What this repository owes `retro` is a `manifests/retro/connector.yaml`. What it owes Chemclaw3 is
listed under "before it can be consumed".

### `retro` — [`chemclaw2_retrosynthesis`](https://github.com/8fqycwdt8v-oss/chemclaw2_retrosynthesis) · port 8854

A meta-model gateway wrapping 40+ open-source retrosynthesis engines (template, transformer, graph,
LLM, multi-step planner, biocatalysis), combining them by reciprocal-rank fusion with RAscore /
SCScore / round-trip boosts and full per-backend provenance. FastAPI, mounted as MCP by
`fastapi-mcp`; one container per engine, so the gateway never imports an ML dependency.

*Tools (explicit `operation_id`s, so these names are stable):* `retrosynthesis_single_step`,
`retrosynthesis_multi_step`, `reaction_forward`, `reaction_classify`, `reaction_conditions`,
`score_synthesizability`, `backends_list`, `healthz`, `version`.

### `rxnpredict` — **forked into this repository**, see tranche 1 above

`chemclaw2_forward` is no longer consumed where it stands: it is forked into `servers/rxnpredict/`
and adapted to this fleet's standards. The entry in tranche 1 is authoritative.

### What this does to the rest of the catalogue

- **`retro` is no longer a build item.** The entry above is kept as a pointer.
- **`rxnsearch` stays, and stays narrow.** It answers "what did people actually run" from ORD
  precedent; `reaction_conditions` and `predict_reaction_conditions` answer "what would a model
  suggest". Two different questions, and the distinction is worth keeping in the tool names.
- **`reaction_classify` overlaps Chemclaw3's `rxnfp`.** Both name a reaction. Decide which one the
  agent should reach for before both are enabled, or the model will be offered two answers to one
  question — the failure `CLAUDE.md`'s duplication rule exists to prevent.
- **`score_synthesizability` is new capability** with no counterpart anywhere in Chemclaw3.

### Before `retro` can be consumed

1. **It ships no `connector.yaml`.** Built for chemclaw2's `mcpServers` JSON config, which
   Chemclaw3 has removed — and because its settings are `extra="forbid"`, exporting
   `CHEMCLAW_MCP_SERVERS` now aborts startup rather than being ignored. It needs a manifest with
   every tool classified `read_only` / `state_changing`. Most are read-only.
2. **Verify the credential is enforced on `/mcp`, not just on the REST routes.** `chemclaw-retro`
   applies its bearer check as `Depends(require_token)` on each router, and its MCP surface is
   *mounted* — a mount bypasses the enclosing app's dependencies. That is precisely the defect
   Chemclaw3 recorded on its own connector fleet (a secret mounted, the control recorded as
   enabled, every tool served to anything that could reach the pod), and the one found in
   `chemclaw2_forward` and fixed by forking it. It may be fine here, because `fastapi-mcp` may
   re-enter the route — but it must be *checked against a running server*, not read off the source.
3. **`retrosynthesis_multi_step` takes minutes.** It is a Chemclaw3 durable job (`jobs:` in the
   manifest), not a synchronous tool. This is the first entry in the catalogue that needs one.
4. **East-west traffic is not egress, and the distinction has to be drawn explicitly.** The gateway
   calls its own backend microservices over HTTP. That is legitimate and unavoidable; what it is
   not is a licence to reach the internet. Its NetworkPolicy admits its own backend Services and
   nothing else, and if it ever adopts `mcp_server_kit`, those Service addresses are what
   `MCP_EGRESS_ALLOW` is for.
5. **Model weights are fetched by a script.** `download_weights.sh` runs at build time, outside the
   serving image — exactly the sanctioned pattern, and the same one `servers/rxnpredict/` now uses.
   It must not become a first-request lazy download.

## Tranche 2 — Compound identity and reference data

*Port note:* 8860 was this tranche's first slot and is now taken by the built `calc` server above, so
`nomenclature` moved to 8864. The block is still 8860+; only the free slots shifted.

| Server | Port | Status | Tools (proposed) | Offline source |
| --- | --- | --- | --- | --- |
| `nomenclature` | 8864 | **next** | `iupac_name_to_structure`, `structure_to_inchi`, `validate_cas`, `normalize_identifier` | OPSIN 2.8.0 (MIT), runs locally. **Re-derived 2026-08-25 and promoted to `next`:** `py2opsin` (MIT, PyPI) bundles the OPSIN jar in the wheel and has *zero Python dependencies*, so the whole corpus arrives at `pip install` time and the no-egress story needs no vendoring step at all — the only image cost is a JRE (Java 8+). Still the best value-to-effort ratio here, and now with a concrete route. |
| `pubchem` | 8861 | proposed | `resolve_identifier`, `compound_properties`, `synonyms`, `cross_references` | A vendored PubChem subset; PubChem's own data is public domain. |
| `chembl` | 8862 | proposed | `search_by_structure`, `bioactivities`, `target_lookup` | A ChEMBL slice. **CC-BY-SA — attribution obligations; needs a licence review before it is built.** |
| `solidform` | 8863 | proposed | `search_structures`, `unit_cell`, `simulate_powder_pattern`, `polymorph_precedent` | Crystallography Open Database (CC0) + pymatgen. The CSD is commercial and out of scope. |

## Tranche 3 — Safety, tox and regulatory

| Server | Port | Status | Tools (proposed) | Offline source |
| --- | --- | --- | --- | --- |
| `ghs` | 8870 | proposed | `hazard_classification`, `h_and_p_statements`, `pictograms`, `exposure_limits` | PubChem LCSS extract + ECHA C&L. **GESTIS forbids transfer into other information systems — do not vendor it.** |
| `reactivity` | 8871 | proposed | `screen_incompatibilities`, `reactive_group_of`, `gas_generation_risk` | NOAA CAMEO Chemicals reactivity matrix (US Government, public domain). |
| `regdocs` | 8872 | proposed | `search_guidance`, `cite_passage`, `limit_lookup` | Vendored ICH (Q3A/Q3C/Q3D/M7/Q7/Q11) and FDA nitrosamine/NDSRI guidance, chunked and cited. |
| `admet` | 8873 | proposed | `predict_admet_panel`, `tox_alerts` | Local open models (ADMET-AI / DeepChem, trained on 41 TDC datasets). ADMETlab 3.0 has an API but is a hosted service — excluded by the no-egress rule. **Re-derived 2026-08-25: Boltz-2 does not replace this**, and the suggestion that it might was wrong. Boltz-2 is MIT and excellent, but it is a co-folding structure-and-binding-affinity model — protein-ligand — and ADMET is a secondary capability of its atom-level representations. A property panel and a binding-affinity predictor are two servers, and only the first is a process-chemistry question. |

## Tranche 4 — Literature and IP

| Server | Port | Status | Tools (proposed) | Offline source |
| --- | --- | --- | --- | --- |
| `litsearch` | 8880 | proposed | `search_literature`, `fetch_abstract`, `resolve_doi`, `citation_graph` | A local index built from Europe PMC / OpenAlex / Crossref bulk dumps at build time. Gives Chemclaw3's existing `deep-research` skill a real index. |
| `patents` | 8881 | proposed | `search_patents`, `patent_chemistry`, `family_and_status`, `claims_text` | SureChEMBL bulk snapshot (chemistry mined from USPTO/WIPO/EPO/JPO/CNIPA). EPO OPS is a live API and is out of scope. |

## Tranche 5 — Spectra and analytics

| Server | Port | Status | Tools (proposed) | Offline source |
| --- | --- | --- | --- | --- |
| `spectra` | 8890 | proposed | `predict_nmr_shifts`, `match_ms_spectrum`, `fragment_formula`, `impurity_mass_id` | nmrshiftdb2 (open) + MassBank (CC-BY) snapshots. Feeds Chemclaw3's `computed-spectra-comparison` skill. |
| `chromatography` | 8891 | proposed | `predict_log_k`, `gradient_scouting_plan`, `method_transfer_scale` | First-party retention models. |

---

## Open questions to settle before the tranches that touch them

- **ChEMBL is CC-BY-SA.** Attribution obligations follow the data into anything derived from it.
  Needs a licence review before `chembl` is built.
- **GESTIS prohibits transfer into other information systems.** `ghs` must be built on PubChem LCSS
  and ECHA C&L, not GESTIS.
- **Snapshot refresh is an operational commitment.** Every mirrored corpus needs a named owner and
  a cadence, recorded in that server's README. A stale patent index that nobody knows is stale is
  worse than no patent index.
