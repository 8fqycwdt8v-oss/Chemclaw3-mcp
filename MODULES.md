# The module catalogue

Every MCP server this fleet has or plans, and the port it owns. **This file is the only port
registry**: claim the next free port in **8850–8899** here, in the pull request that adds the server.
Status is `built`, `adopted`, `next` (agreed, queued) or `proposed`. **Offline** says what a server
reads, because production has no egress. Nothing here duplicates a Chemclaw3 capability (`CLAUDE.md`).

## Built

| Server | Port | Status | What it answers | Offline source |
| --- | --- | --- | --- | --- |
| `props` | 8850 | **built** | Solvent and pure-component properties, vapour pressure, Hansen-ranked swap shortlist. | Vendored, checksummed CSV compiled here (CC0). |
| `thermalsafety` | 8851 | **built** | Runaway and thermal-hazard arithmetic. | First-party formulas plus `molmass` atomic weights; no corpus. |
| `kinetics` | 8852 | **built** | Isothermal rate and ideal-reactor arithmetic. | First-party formulas; no corpus. |
| `unitops` | 8853 | **built** | Scale-up and unit-operation sizing. | First-party correlations; no corpus. |
| `rxnpredict` | 8857 | **built** | Forward reaction and condition prediction, a Borda-weighted ensemble with per-model spread. | Model checkpoints baked in at build time. |
| `chem` | 8858 | **built** | Bench chemistry over RDKit: compound resolution, stoichiometry, green metrics, depiction, torsions, species and degradant enumeration. | Vendored reagent CSV (CC0) plus RDKit. |
| `safety` | 8859 | **built** | Cited hazard, genotoxicity and ICH impurity tables. | Vendored, checksummed YAML corpora plus RDKit. |
| `calc` | 8860 | **built** (backend) | The semiempirical physics behind Chemclaw3's calculators (GFN2-xTB, CREST), as keyed primitives. | No dataset; programs and parameters in the image. |
| `rxnlabel` | 8865 | **built** (backend) | Reaction and species representations, roles and named reactions. | RXNMapper checkpoint inside its hashed wheel. |
| `suitability` | 8892 | **built** | USP <621> system suitability arithmetic from numbers a run already produced. | First-party formulas; no corpus. |
| `pyexec` | 8899 | **built** | A bounded, offline Python analysis sandbox. | No dataset. |

Each server's `README.md` is the authority on its tools, bounds and scope; its `connector.yaml` on its
surface. `calc` and `rxnlabel` are registered in `manifests-internal/`.

## What the built fleet costs to run

Read off each `servers/<name>/deploy/deployment.yaml` and `hpa.yaml`; when one moves, this table
moves in the same pull request.

| server | replicas (min → max) | requests/pod | baseline (min x req) | ceiling (max x req) |
| --- | --- | --- | --- | --- |
| `props` | 2 → 4 | 250m / 256Mi | 0.5 CPU, 0.5 Gi | 1 CPU, 1 Gi |
| `chem` | 2 → 6 | 500m / 256Mi | 1 CPU, 0.5 Gi | 3 CPU, 1.5 Gi |
| `safety` | 2 → 4 | 250m / 256Mi | 0.5 CPU, 0.5 Gi | 1 CPU, 1 Gi |
| `rxnlabel` | 2 → 4 | 500m / 1Gi | 1 CPU, 2 Gi | 2 CPU, 4 Gi |
| `rxnpredict` | 2 → 4 | 500m / 2Gi | 1 CPU, 4 Gi | 2 CPU, 8 Gi |
| `pyexec` | 2 → 6 | 1 / 512Mi | 2 CPU, 1 Gi | 6 CPU, 3 Gi |
| `calc` | 2 → 8 | 1 / 1Gi | 2 CPU, 2 Gi | 8 CPU, 8 Gi |
| `thermalsafety` | 2 → 4 | 250m / 256Mi | 0.5 CPU, 0.5 Gi | 1 CPU, 1 Gi |
| `kinetics` | 2 → 4 | 250m / 256Mi | 0.5 CPU, 0.5 Gi | 1 CPU, 1 Gi |
| `unitops` | 2 → 4 | 250m / 256Mi | 0.5 CPU, 0.5 Gi | 1 CPU, 1 Gi |
| `suitability` | 2 → 4 | 250m / 256Mi | 0.5 CPU, 0.5 Gi | 1 CPU, 1 Gi |
| **fleet** | | | **10 CPU, 12.5 Gi** | **27 CPU, 30.5 Gi** |

## Tranche 1 — Route and process engineering, still open

| Server | Port | Status | Tools (proposed) | Offline source |
| --- | --- | --- | --- | --- |
| `retro` | 8854 | **adopted** | `retrosynthesis_single_step`, `retrosynthesis_multi_step`, `reaction_forward`, `reaction_classify`, `reaction_conditions`, `score_synthesizability` | Hosted in [`chemclaw2_retrosynthesis`](https://github.com/8fqycwdt8v-oss/chemclaw2_retrosynthesis); weights fetched at build time. Owed: a manifest here (see `docs/BACKLOG.md`). |
| `rxnsearch` | 8855 | proposed | `conditions_for_transformation`, `yield_distribution`, `reagent_frequency`, `precedent_count` | Open Reaction Database snapshot (CC-BY). Aggregate statistics only: per-record ORD retrieval is Chemclaw3's. |
| `blocks` | 8856 | proposed | `search_building_blocks`, `price_and_lead_time`, `route_cost_rollup` | A mounted catalogue export, never a vendor API. |

## Tranche 2 — Compound identity and reference data

| Server | Port | Status | Tools (proposed) | Offline source |
| --- | --- | --- | --- | --- |
| `nomenclature` | 8864 | **next** | `iupac_name_to_structure`, `structure_to_inchi`, `validate_cas`, `normalize_identifier` | OPSIN via `py2opsin` (MIT), the jar inside the wheel; needs a JRE. |
| `pubchem` | 8861 | proposed | `resolve_identifier`, `compound_properties`, `synonyms`, `cross_references` | A vendored PubChem subset; PubChem's own data is public domain. |
| `chembl` | 8862 | proposed | `search_by_structure`, `bioactivities`, `target_lookup` | A ChEMBL slice. **CC-BY-SA: needs a licence review before it is built.** |
| `solidform` | 8863 | proposed | `search_structures`, `unit_cell`, `simulate_powder_pattern`, `polymorph_precedent` | Crystallography Open Database (CC0) + pymatgen. |

## Tranche 3 — Safety, tox and regulatory

| Server | Port | Status | Tools (proposed) | Offline source |
| --- | --- | --- | --- | --- |
| `ghs` | 8870 | proposed | `hazard_classification`, `h_and_p_statements`, `pictograms`, `exposure_limits` | PubChem LCSS extract + ECHA C&L. **GESTIS forbids transfer into other information systems — do not vendor it.** |
| `reactivity` | 8871 | proposed | `screen_incompatibilities`, `reactive_group_of`, `gas_generation_risk` | NOAA CAMEO Chemicals reactivity matrix (US Government, public domain). |
| `regdocs` | 8872 | proposed | `search_guidance`, `cite_passage`, `limit_lookup` | Vendored ICH (Q3A/Q3C/Q3D/M7/Q7/Q11) and FDA nitrosamine/NDSRI guidance, chunked and cited. |
| `admet` | 8873 | proposed | `predict_admet_panel`, `tox_alerts` | Local open models (ADMET-AI / DeepChem). ADMETlab 3.0 is a hosted API and is excluded. |

## Tranche 4 — Literature and IP

| Server | Port | Status | Tools (proposed) | Offline source |
| --- | --- | --- | --- | --- |
| `litsearch` | 8880 | proposed | `search_literature`, `fetch_abstract`, `resolve_doi`, `citation_graph` | A local index built from Europe PMC / OpenAlex / Crossref bulk dumps at build time. |
| `patents` | 8881 | proposed | `search_patents`, `patent_chemistry`, `family_and_status`, `claims_text` | SureChEMBL bulk snapshot. EPO OPS is a live API and is out of scope. |

## Tranche 5 — Spectra and analytics

| Server | Port | Status | Tools (proposed) | Offline source |
| --- | --- | --- | --- | --- |
| `spectra` | 8890 | proposed | `predict_nmr_shifts`, `match_ms_spectrum`, `fragment_formula`, `impurity_mass_id` | nmrshiftdb2 (open) + MassBank (CC-BY) snapshots. |
| `chromatography` | 8891 | proposed | `predict_log_k`, `gradient_scouting_plan`, `method_transfer_scale` | First-party retention models; predicts, where `suitability` checks. |
