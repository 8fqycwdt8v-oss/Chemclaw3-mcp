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

### `thermalsafety` — runaway and thermal-hazard arithmetic · port 8851 · **next**

The calculations behind a safe scale-up, from calorimetry numbers the chemist supplies: adiabatic
temperature rise, MTSR, TMR_ad, SADT, the Stoessel criticality class, heat-removal capacity, and an
oxygen-balance screen for energetic functionality.

*Proposed tools:* `adiabatic_temperature_rise`, `mtsr`, `tmr_ad`, `sadt`,
`stoessel_criticality_class`, `heat_removal_capacity`, `oxygen_balance_screen`.
*Offline:* first-party formulas; no corpus needed.
*Note:* complements Chemclaw3's `safety` connector, which screens *structures* for hazard alerts.
This one takes DSC/RC1/ARC numbers and answers "what happens if the cooling fails".

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

### `retro` — retrosynthesis and route scoring · port 8854 · proposed

*Proposed tools:* `plan_routes`, `single_step_disconnections`, `is_purchasable`, `score_route`.
*Offline:* AiZynthFinder (MIT) with USPTO templates and policy models baked into the image.
*Note:* minutes of CPU per search. **Declare it as a Chemclaw3 durable job**, not a synchronous
tool — the first server here that will need a `jobs:` entry, and therefore the first pull request
that touches both repositories.

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

## Tranche 2 — Compound identity and reference data

| Server | Port | Status | Tools (proposed) | Offline source |
| --- | --- | --- | --- | --- |
| `nomenclature` | 8860 | proposed | `iupac_name_to_structure`, `structure_to_inchi`, `validate_cas`, `normalize_identifier` | OPSIN (MIT), runs locally. Zero licence risk and the best value-to-effort ratio in the catalogue — a strong queue-jumper. |
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
