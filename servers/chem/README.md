# `chem` — bench chemistry over RDKit

What do I weigh out, what is this compound, what does it look like, and how green is this route.
Four pure, deterministic tools over RDKit and a vendored table of 61 bench reagents.

## It replaces a Chemclaw3 bundle rather than adding a second one

Chemclaw3 ships its own in-tree `chem` connector, and `CLAUDE.md`'s exclusion table forbids a second
answer to one question. This is a **port**, not a duplicate: same manifest `name`, same four tools,
same argument names, same docstrings — the model-facing prose is carried over word for word, because
several sentences in it exist to prevent a mistake that was measured in a live run.

The two cannot both answer, and that is enforced by Chemclaw3's own mechanism rather than by
convention:

- Bundles are addressed by name, so `CHEMCLAW_CONNECTOR_URLS` has one `chem` key.
- `CHEMCLAW_CONNECTORS_DIR` is a `PATH`-style list and **the first directory wins a name
  collision** (`connectors/registry.py::_bundle_dirs`, "first dir wins"). Putting this fleet's
  `manifests/` ahead of Chemclaw3's own connectors directory is what makes this server the `chem`
  the agent sees; leaving it behind keeps the in-tree bundle.

Moving it out here buys what the split is for: RDKit leaves the chat service's image, and the tool
surface releases on its own cadence.

## Tools

| Tool | Answers |
| --- | --- |
| `resolve_compound` | The name a chemist wrote → a canonical structure, or an honest miss. |
| `stoichiometry_table` | What to weigh and measure out for a batch, scaled to the basis. |
| `green_metrics` | E-factor and PMI from the charged masses. |
| `render_structure` | A molecule or reaction as an inline SVG. |

All four are `read_only`: pure functions of their arguments plus a read of a read-only table.
Nothing here writes, spends real compute, or has an effect worth gating — which matters, because
"what do we actually charge, and what does it cost in waste" has to be answerable *before* a plan is
approved, not after.

## Running it

```sh
make run-chem                             # from the repository root; 127.0.0.1:8858
curl -s localhost:8858/healthz            # {"status":"ok","server":"chem"}
```

The bearer token is `CHEMCLAW_CHEM_TOKEN`, and the same variable name is read on both sides.
Chemclaw3's in-tree bundle declares `auth: {mode: none}` because it was only ever dialled over
loopback from the same pod; a server in another image is dialled across a network, so this one
declares bearer and enforces it even on the loopback dev URL.

`CHEMCLAW_CHEM_RENDER_SIZE_PX` (default 320) is the depiction's edge length in pixels — Chemclaw3
carries the same knob as `settings.structure_render_size_px`, for deployments whose chat surface
renders larger cards.

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
Chemclaw3's; none of these four tools ever asked that question.

## What it is not

It knows nothing about hazard, reactivity or whether a route will work. A charge table is
arithmetic over molecular weights and densities: it will happily scale a reagent that decomposes
under the conditions, and E-factor and PMI say nothing about toxicity, energy or cost. Hazard
screening is Chemclaw3's `safety` connector; solvent properties are `props`.
