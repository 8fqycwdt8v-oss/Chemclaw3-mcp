# The module catalogue

Every MCP server this fleet has or plans, why it earns a place, and the port it owns. This file is
the authoritative port registry: claim the next free port here in the same pull request that adds
the server.

**Status** is one of `built`, `next` (agreed, queued), or `proposed` (in the catalogue, not yet
argued through). **Offline** says what the server reads, because production has no egress — see
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
`solvent_swap_candidates`, `compare_solvents` — all `read_only`.
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

### `chem` — bench chemistry over RDKit · port 8858 · **built**

What do I weigh out, what is this compound, what does it look like, and how green is this route.
`resolve_compound` turns the name a chemist wrote (`DIPEA`, `Pd(dppf)Cl2`, `2-MeTHF`) into a
canonical structure — the bridge every structure-taking tool in the fleet needs;
`stoichiometry_table` scales a batch to the limiting reagent and converts solvent *volumes* into
real masses; `green_metrics` computes E-factor and PMI from exactly those masses; `render_structure`
draws a molecule or reaction as an inline SVG.

*Tools:* `resolve_compound`, `stoichiometry_table`, `green_metrics`, `render_structure` — all
`read_only`.
*Offline:* a vendored, checksummed CSV of 61 reagents under 87 spellings (CC0), plus RDKit. No
upstream at all, and deliberately so: an external resolver (PubChem, OPSIN) is a request-time
network call, which this repository does not permit and which the common case does not need.
*Provenance:* **a port of Chemclaw3's own in-tree `chem` connector** — the same manifest name, the
same four tools, the same argument names, the same model-facing docstrings. It is a replacement for
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

- **`rules.yaml` has no licence and its `dataset.json` records that as UNRESOLVED.** Every rule
  carries a citation and the file carries no licence statement, in this repository or the one it came
  from. It is very likely first-party — original SMARTS written against cited hazard literature
  rather than a transcribed table — but that is an explanation, not a grant, and a test pins the
  string so nobody quietly types `CC0` to make the loader happy. **This is open and a reviewer has
  to settle it.**
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

### `calc` — GFN2-xTB, pKa, solubility, logD and developability descriptors · port 8860 · **built**

The fast local calculators, as nine request/response tools. `compute_xtb_energy` is a GFN2-xTB
single point; `compute_electronic_properties` reads the same SCF's HOMO/LUMO/gap, dipole, Mulliken
charges and Wiberg bond orders; `predict_site_reactivity` ranks atoms by condensed Fukui index;
`optimize_geometry` relaxes to a stationary point of the GFN2 surface; `compute_thermochemistry`
takes the Hessian on top of it for frequencies, an IR spectrum and ideal-gas RRHO free energy.
Beside them: the xTB pKa predictor (acids, and aromatic/aryl-nitrogen bases), an ESOL solubility
baseline with an applicability-domain check, pH-dependent logD, and an RDKit developability panel.

*Tools:* `compute_xtb_energy`, `compute_electronic_properties`, `predict_site_reactivity`,
`optimize_geometry`, `compute_thermochemistry`, `predict_pka`, `predict_solubility`, `predict_logd`,
`predict_developability_profile` — all `state_changing`, matching Chemclaw3's own manifest. That
reads oddly for a stateless server and is still right: `read_only` is a *gate* (the plan gate lets an
unapproved plan call one), and these are minutes of CPU each with no cache underneath. Plus
`calculation_key`, the tenth and the only `read_only` one: it returns what a calculation *would* be
stored under, without running it, which is what lets the caller's cache answer "have I already
computed this?" before paying for it.
*Offline:* **no vendored dataset at all** — the first server in the fleet with none. Every number is
computed from tblite's compiled GFN parameters, RDKit's Crippen/QED tables and closed-form
arithmetic, all of which arrive inside their own wheels. `tests/test_no_egress.py` proves sufficiency
by running one of each kind of calculation with the guard armed, rather than by pointing at a corpus.
*Provenance:* **a port of nine of the fifteen tools on Chemclaw3's own in-tree `calc` connector**,
same names, same arguments, same model-facing docstrings minus every sentence that claimed a result
was cached.

**This is the one server in the fleet that is not a connector Chemclaw3 dials, and the difference is
load-bearing.** `chem` and `safety` carry their bundle's whole surface, so registering them on
`CHEMCLAW_CONNECTORS_DIR` swaps one implementation for an identical one. Six `calc` tools cannot
move — `report_measurement`, `calculator_trust`, `calculator_outliers` (the calibration ledger),
`find_calculations` (the calculation cache), `list_artifacts`/`fetch_artifact` (the artifact store) —
nor can any of the bundle's durable jobs. So Chemclaw3 keeps its `calc` bundle and all fifteen tools,
and calls this server from inside `science/calc/store.py::cached_compute` as a **backend on a miss**.
Registering it as a connector would let a partial port win the name collision and remove those six
tools and every durable job from the agent's surface, with no error. See `docs/integration.md`.

**The cache stayed behind, so its addressing had to travel — in both directions.** Every result
carries `calc_version` and (on eight of the nine) `calc_key`, the full
`calc_type@calc_version:input_hash:params_hash` string. But `cached_compute` takes the key as an
*argument*, so a key that arrives only on the result cannot serve the lookup — hence
`calculation_key`, which answers the same question one round trip earlier and cheaply (canonicalise,
embed, hash; no SCF, asserted by making every path through `Calculator` raise).

Why it must be derived here at all: those strings are assembled from the installed `tblite`/`rdkit`
distribution versions, a Hamiltonian-revision constant, an `xtb --version` subprocess, and (for pKa)
seven calibration settings — none of which exist on a Chemclaw3 pod after the split. And a local
reconstruction would not fail loudly: `calc_version` is the primary key of the calibration ledger
(`predictions`, unique on `(calc_type, calc_version, input_hash)`, matched exactly with no version
pooling), and `xtb_cli.binary_version()` returns the literal string `"absent"` rather than raising,
so the string comes out well-formed, matches zero rows, and `calculator_trust("pka")` reports
`UNCALIBRATED`. Silent, not loud.

`tests/test_calculation_key.py` asserts that the key derived up front is the key the compute tool
stamps on its result, for every tool — the property the whole design rests on.
`tests/test_calc_version.py` asserts the field on all nine; `tests/test_key_contract.py` pins the
hash, the epoch and the key format against literal strings taken from Chemclaw3.

**Three things to know before touching it:**

- **`xtb` and `crest` are not in the image and cannot be**, being compiled Fortran distributed
  through conda-forge rather than PyPI. This costs the ANCopt speedup (~7-9x on 76-118 atoms) and
  GFN-FF, and nothing else: `tblite` carries the same Hamiltonians in-process and Chemclaw3's own
  deployment resolves to it too, so the numbers and the version strings are unchanged by the port —
  verified by deriving both from the two trees on the same package versions. `engine/xtb_cli.py` is
  ported in full; installing the binary is one image layer and the `auto` default finds it.
- **Three definitions are copied from Chemclaw3**: `stable_hash`,
  `CalculationKey`/`CALCULATION_EPOCH`, and `require_canonical_smiles`. Unlike `chem`'s and
  `safety`'s copies of the last one, these *do* derive keys, and each is pinned by a contract test.
  But because Chemclaw3 never derives a key itself — `calculation_key` hands it the four fields
  `store.get` takes — **only `CALCULATION_EPOCH` actually has to agree across the two repositories**,
  and it does because Chemclaw3 still keys its own in-tree calculators into the same table. It is a
  source constant in both and moves in both or in neither.
- **The calculator settings do *not* have to agree**, and that is a consequence of the same design
  rather than a coincidence. `engine/config.py` uses Chemclaw3's env prefix and field names because
  an operator configuring both should not have to learn a second vocabulary — but seven of those
  values are inside the pKa version string, and only this server reads them, so tuning a calibration
  here moves the version everywhere it appears, consistently. Same for the RDKit build under
  `structure_id`: only this server embeds.

**What was dropped in the port:** the calculation cache and artifact store (`store.py`,
`postgres_*.py`, `artifacts.py`), the calibration ledger (`calibration.py`), every `run_cached_*`
wrapper, `geometry.py`'s cross-method pointer, `crest_cli.py` with `CrestSpec`, and the whole
durable-job half of the bundle (`complexes`, `conformers`, `reaction`, `xtb_scan`, `specs`,
`results`, `activities`, `workflows`, `worker`). See `servers/calc/README.md` for the full table and
for the two behaviours that moved *into* the calculators when their cached wrappers were deleted.

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
| `nomenclature` | 8864 | proposed | `iupac_name_to_structure`, `structure_to_inchi`, `validate_cas`, `normalize_identifier` | OPSIN (MIT), runs locally. Zero licence risk and the best value-to-effort ratio in the catalogue — a strong queue-jumper. |
| `pubchem` | 8861 | proposed | `resolve_identifier`, `compound_properties`, `synonyms`, `cross_references` | A vendored PubChem subset; PubChem's own data is public domain. |
| `chembl` | 8862 | proposed | `search_by_structure`, `bioactivities`, `target_lookup` | A ChEMBL slice. **CC-BY-SA — attribution obligations; needs a licence review before it is built.** |
| `solidform` | 8863 | proposed | `search_structures`, `unit_cell`, `simulate_powder_pattern`, `polymorph_precedent` | Crystallography Open Database (CC0) + pymatgen. The CSD is commercial and out of scope. |

## Tranche 3 — Safety, tox and regulatory

| Server | Port | Status | Tools (proposed) | Offline source |
| --- | --- | --- | --- | --- |
| `ghs` | 8870 | proposed | `hazard_classification`, `h_and_p_statements`, `pictograms`, `exposure_limits` | PubChem LCSS extract + ECHA C&L. **GESTIS forbids transfer into other information systems — do not vendor it.** |
| `reactivity` | 8871 | proposed | `screen_incompatibilities`, `reactive_group_of`, `gas_generation_risk` | NOAA CAMEO Chemicals reactivity matrix (US Government, public domain). |
| `regdocs` | 8872 | proposed | `search_guidance`, `cite_passage`, `limit_lookup` | Vendored ICH (Q3A/Q3C/Q3D/M7/Q7/Q11) and FDA nitrosamine/NDSRI guidance, chunked and cited. |
| `admet` | 8873 | proposed | `predict_admet_panel`, `tox_alerts` | Local open models (ADMET-AI / DeepChem). ADMETlab 3.0 has an API but is a hosted service — excluded by the no-egress rule. |

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
