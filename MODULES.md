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
*Bounds:* both correlations stop at 400 °C, and `boiling_point_at_pressure` refuses a pressure the
solvent does not reach below it — it used to return the end of its own bisection instead, answering
"400.0 °C" for toluene at 200 bar and at 10 000 bar alike. The swap filters are closed vocabularies
in the tool signature, so a misspelled constraint is refused rather than blocking every candidate
with a reason that reads like chemistry.

### `rxnpredict` — forward reaction & condition prediction · port 8857 · **built**

What will this reaction give, and how would people run it? Several open-source predictors run in
parallel and their ranked outputs are combined by Borda-weighted voting, gated on a coarse SMARTS
reaction class. The per-model spread comes back with the consensus, because "four of five agree"
and "only the rule-based one produced this" are different answers.

*Tools:* `predict_forward_reaction`, `predict_reaction_conditions`, `predict_forward_single_model`,
`predict_conditions_single_model`, `list_available_models`, `classify_reaction` — all `read_only`.
*Offline:* model checkpoints baked in at build time (`scripts/fetch_models.py` in a builder stage),
with `HF_HUB_OFFLINE=1` at runtime and their digests verified after the `COPY`; per-class trust
priors as a checksummed vendored dataset.
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

**What a later review of this fork found and fixed**, recorded because each was a control that read
as enforced and was not: `DISABLED_MODELS` was honoured by the consensus tools and bypassed by the
single-model ones, which also advertised the disabled predictor as usable; an empty result from a
caller's `models` filter was reported as "no predictors are available in this deployment", sending
the agent to diagnose a server that was fine; and this server's source was outside the type-checked
set entirely — `make check` was green over 15 files with 28 unchecked.

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
