# `chem` — bench chemistry over RDKit

What do I weigh out, what is this compound, what does it look like, which bond can I rotate, how
green is this route — and, for Chemclaw3's multi-step protocols, which species a structure can exist
as. Pure, deterministic tools over RDKit and a vendored table of 61 bench reagents; port **8858**.

## Where it came from, and what Chemclaw3 holds of it

This began as a **port** of Chemclaw3's in-tree `chem` connector, not a duplicate: same manifest
`name`, same tools, same argument names, and the model-facing rules carried over intact, because
several of them exist to prevent a mistake that was measured in a live run. The prose has since been
narrowed to the rules themselves (Chemclaw3-mcp#152): every model call that binds this server pays
for it, so the measurement that earned a rule lives in the test that holds it, and
`tests/test_prompt_cost.py` ratchets what the surface costs. `enumerate_torsions` and the species
enumerations were added here after the port.

Chemclaw3 no longer runs any `chem` code and holds no copy of its manifest: it installs
`connector.yaml` from the pinned `chemclaw-contracts` package, because its validators and skills name
these tools by string. So there is one implementation, one manifest and one address
(`CHEMCLAW_CONNECTOR_URLS` has one `chem` key); a second `chem` manifest on `CHEMCLAW_CONNECTORS_DIR`
is a startup error (`docs/integration.md`).

Moving it out here buys what the split is for: RDKit leaves the chat service's image, and the tool
surface releases on its own cadence.

## Tools

| Tool | Answers |
| --- | --- |
| `resolve_compound` | The name a chemist wrote → a canonical structure, an honest miss said in words (with near-miss suggestions from the table, and no name service behind it), or a refusal where the name reads as two substances (`CO` is carbon monoxide *and* the SMILES for methanol). |
| `stoichiometry_table` | What to weigh and measure out for a batch, scaled to the basis. |
| `green_metrics` | E-factor and PMI from the charged masses. |
| `render_structure` | A molecule or reaction as an inline SVG, optionally with atoms highlighted. |
| `enumerate_torsions` | Which bonds can be rotated, each with a handle that survives a rewritten SMILES. |
| `describe_topology` | What the molecular graph is like, before an expensive search is spent on it — whether a search would find anything at all. |
| `describe_sites` | A name for every atom, so a per-atom number can be reported as a *position*. |
| `enumerate_tautomers` | The proton-shift isomers a molecule can exist as. |
| `enumerate_protonation_states` | The protonation microstates — each ionisable site toggled, one at a time. |
| `enumerate_stereoisomers` | The stereoisomers at the centres a SMILES leaves unassigned. |
| `enumerate_bond_cleavages` | Every breakable bond and the two fragments breaking it would give. |
| `enumerate_degradants` | Degradation products proposed by forced-degradation transforms. |
| `enumerate_substitutions` | The regioisomers of a substitution series — the set a "which position" question ranks. |

All of them are `read_only`: pure functions of their arguments plus a read of a read-only table, and
nothing here writes — which matters, because "what do we actually charge, and what does it cost in
waste" has to be answerable *before* a plan is approved, not after. **Read-only is not free**, though:
`render_structure`, `describe_topology` and the tautomer, protonation, stereoisomer, degradant and
substitution enumerations cost up to seconds of CPU on a legal molecule, so those seven share one
admission ceiling (`engine/admission.py::GATED_TOOLS`) and the manifest lists them under `queued:` so
Chemclaw3 waits for a slot rather than being refused. Each enumeration bounds its output and
**refuses past the bound rather than truncating**.

## Running it

```sh
make run-chem                             # from the repository root; 127.0.0.1:8858
curl -s localhost:8858/healthz            # {"status":"ok","server":"chem",…,"datasets":["bench-reagents@0.1.0"]}
```

The bearer token is `CHEMCLAW_CHEM_TOKEN`, and the same variable name is read on both sides. It is
enforced even on the loopback dev URL; the in-tree bundle this was ported from declared
`auth: {mode: none}` because it was only ever dialled over loopback from the same pod.

## Operating it

Build, deploy, wiring and the fleet-wide variables are in
[`docs/operations.md`](../../docs/operations.md); what is particular to this server:

| | |
| --- | --- |
| Port / Service | 8858 / `chemclaw-mcp-chem` |
| Token | `CHEMCLAW_CHEM_TOKEN` |
| Chemclaw3 | connector `chem`, manifest installed from `chemclaw-contracts`, enabled by default; its seven queued tools run through `connectors.chem.interactive` (sized 2 pods x 4 slots) |
| Pod | requests 500m / 256Mi, limits 1 CPU / 512Mi; 2 → 6 replicas on CPU (or KEDA on admission, `deploy/keda/`) |
| Readiness | `/healthz` loads and checksums `records.csv` (`bench-reagents@<version>`); a corrupt table is a 503 naming the file. |
| Admission | `CHEMCLAW_CHEM_MAX_CONCURRENT_HEAVY_CALLS` (4, derived from the pod's thread pool) over the seven gated tools; a full pod answers `[chem-at-capacity] …`. `CHEMCLAW_CHEM_MAX_CONCURRENT_RENDERS` is retired and refused at startup. |

Every bound below is a startup-validated environment variable, reported under `bounds` on
`/healthz`:

| Variable | Default | Bounds |
| --- | --- | --- |
| `CHEMCLAW_CHEM_RENDER_SIZE_PX` | 320 | depiction edge length in pixels |
| `CHEMCLAW_CHEM_MAX_DEPICTION_ATOMS` | 250 | atoms in one drawing (2D layout is superlinear) |
| `CHEMCLAW_CHEM_MAX_DEPICTION_CHARS` | 50,000 | SVG characters returned |
| `CHEMCLAW_CHEM_MAX_SITE_ATOM_PRODUCT` | 150,000 | ionisable sites x atoms, for `enumerate_protonation_states` |
| `CHEMCLAW_CHEM_MAX_TAUTOMER_HEAVY_ATOMS` | 500 | heavy atoms for tautomer enumeration |
| `CHEMCLAW_CHEM_MAX_STEREO_ISOMER_ATOM_PRODUCT` | 6,000 | unassigned centres x atoms, for `enumerate_stereoisomers` |
| `CHEMCLAW_CHEM_MAX_DEGRADANT_MATCH_ATOM_PRODUCT` | 100,000 | transform matches x atoms, for `enumerate_degradants` |
| `CHEMCLAW_CHEM_MAX_SUBSTITUTION_HEAVY_ATOMS` | 250 | heavy atoms for `enumerate_substitutions` |
| `CHEMCLAW_CHEM_MAX_SUBSTITUTION_CANDIDATE_ATOM_PRODUCT` | 20,000 | candidates x heavy atoms, for `enumerate_substitutions` |

`MCP_MAX_SMILES_CHARS` and `MCP_MAX_MOLECULE_ATOMS` apply before any of these. A call past a bound is
refused with a message naming the size, before the work starts — never truncated.

The rest of this section is why the bounds are priced the way they are.

`CHEMCLAW_CHEM_RENDER_SIZE_PX` (default 320) is the depiction's edge length in pixels — the knob
Chemclaw3 carried as `settings.structure_render_size_px`, for deployments whose chat surface renders
larger cards.

Two bounds sit on `render_structure`, and they bound different things.
`CHEMCLAW_CHEM_MAX_DEPICTION_ATOMS` (default 250) bounds what a drawing costs *this pod*, because
`Compute2DCoords` is superlinear. `CHEMCLAW_CHEM_MAX_DEPICTION_CHARS` (default 50,000) bounds what
leaves it: the SVG grows with the molecule — ethanol 1,995 characters, erythromycin 34,526, a
250-atom chain 126,348, and 244,522 with every atom highlighted — against a caller that cuts one
tool result at 60,000 characters divided by the width of the batch it was called in. A cut SVG
draws nothing, so an oversized depiction is refused whole, the way every enumeration on this server
refuses past its bound rather than returning a partial answer.

`CHEMCLAW_CHEM_MAX_SITE_ATOM_PRODUCT` (default 150,000) is the one bound here that prices CPU
rather than the step after it. `enumerate_protonation_states` toggles, sanitises and canonicalises
each ionisable site separately, so the cost is the site count **times the molecule size** — and the
cap on what it *returns* could not see that, because a molecule whose sites are all equivalent
comes in under the cap after burning the whole time: measured, a 660-amine macrocycle was answered
with two species after **15,647 ms**, and a 660-amine chain was refused after **48,077 ms**, both
inside every other bound this server has. See
`docs/decisions/D-2026-09-18-an-output-cap-is-not-a-bound-on-the-work.md`.

The bound that closed it first priced the **site count alone**, which this paragraph's own sentence
already said was half of it — so it refused a PAMAM G3 dendrimer (484 heavy atoms, 62 sites) that
costs 128 ms and yields six structures while admitting a 1,891-atom polyamine at 31 sites that
costs 413 ms. It now prices the product, the worst call it admits measures **1,266 ms**, and PAMAM
G4 (996 atoms, 126 sites, 587 ms) is answered. See
`docs/decisions/D-2026-09-19-a-bound-on-the-site-count-prices-half-the-work.md`.
`enumerate_substitutions` — the regioisomers of an aromatic substitution series, for "which
position" questions: every substituent moved round its ring (`mode="move"`, the input included) or
one named group put on each aromatic C-H (`mode="add"`, the input excluded) — has two bounds, both
priced before any product is built. `CHEMCLAW_CHEM_MAX_SUBSTITUTION_HEAVY_ATOMS` (default 250)
refuses a molecule whose positions cost too much to *name*, since that and canonicalising each
product grow faster than the molecule; `CHEMCLAW_CHEM_MAX_SUBSTITUTION_CANDIDATE_ATOM_PRODUCT`
(default 20,000) prices `candidates x heavy atoms`, the degradant enumerator's shape. The frontier
both were derived from is in `engine/substitution.py`. A ranking of the set is a *thermodynamic*
order, and the tool's docstring says why that is not a regioselectivity.

`describe_topology` is deliberately outside the bound — it is the tool that answers for a molecule
the enumeration refuses — but it is **not** free and is usually the dearer of the two: 2,793 ms on
PAMAM G4 against the enumeration's 587 ms, because it enumerates tautomers.

## The data

`src/chemclaw_mcp_chem/data/records.csv`, described and checksummed by `dataset.json` beside it.

- **What it is:** 61 substances × 4 columns — the display name, every spelling a chemist writes
  (87 in total), the SMILES, and for the 22 that can be charged by volume an ambient density in
  g/mL. Solvents, amine and inorganic bases, palladium sources and ligands, coupling and activating
  reagents, oxidants and azides.
- **Licence:** CC0-1.0. Ported from Chemclaw3's `chemclaw.core.reagents`, first-party content of
  the same owner.
- **Refreshed by:** whoever changes it, in a pull request. There is no ingestion job and no upstream
  to drift from — the deliberate design decision Chemclaw3 recorded and this port keeps: an external
  resolver (PubChem, OPSIN) is a network round trip, which this repository does not permit and which
  the common case does not need.

**Verify against a primary source before a number here enters a batch record.** It is a working
table for an agent's reasoning, not a certified reference.

### Two things the table is careful about

**A density says "can be charged by volume", not "is a solvent".** Acetic acid at 1.5 equiv, water
in a hydrolysis, methanol in an esterification, DMSO as the Swern oxidant and DMF as the Vilsmeier
reagent all have a density on file and are all routinely charged by molar equivalent. Only the
chemist knows which reading was meant, so `stoichiometry_table` takes the charge in the units it was
*specified* in and reports which those were on each row's `role`. Chemclaw3 got this wrong in the
other direction once — it refused any reagent that had a density — and the comment in its table now
says so.

**A missing solvent is an error, not a missing row.** An unresolvable *reagent* is listed in
`unresolved` and skipped: a chemist reads a charge list line by line and sees it. An unresolvable
solvent, or one with no density on file, raises instead, because a dropped solvent leaves a table
that looks complete while halving the E-factor and PMI computed from its masses.

## The one duplicated definition, and how it is kept honest

`engine/chem.py` is a copy of Chemclaw3's `core/chem.py` canonicalization. It has to be — a server
here cannot import Chemclaw3 — and Chemclaw3's copy is the authority, because that is where D-011's
calculation-cache keys, the QM workflow-dedup id and 26 other importers live. Nothing on this server
derives a key; this copy governs only what this server accepts and echoes.

`tests/test_canonicalization_contract.py` is what makes a divergence *detectable* rather than
silent: a table of representative inputs — tautomers, charged species, stereocentres, a salt, a
kekulized aromatic — with their expected canonical output written out as literal strings, every one
produced by running Chemclaw3's own `require_canonical_smiles`. The same table passes in either
repository without one importing the other, so an RDKit upgrade or a pipeline change on either side
turns a test red instead of quietly answering differently.

**What was deliberately not ported:** the `standardize` pipeline that answers the *other* question,
"is this the same compound?" (salts stripped, charges neutralized, one tautomer per set), and
`compound_id` built on it. They key the knowledge graph and the fingerprint index, which are
Chemclaw3's; none of these tools ever asked that question.

## What it is not

It knows nothing about hazard, reactivity or whether a route will work. A charge table is
arithmetic over molecular weights and densities: it will happily scale a reagent that decomposes
under the conditions, and E-factor and PMI say nothing about toxicity, energy or cost. Hazard
screening is this fleet's `safety` server; solvent properties are `props`.
