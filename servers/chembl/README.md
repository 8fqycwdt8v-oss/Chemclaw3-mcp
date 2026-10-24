# `chembl` — compound lookup and aggregated bioactivity from a ChEMBL subset (CC BY-SA 3.0)

Two questions a chemist asks of ChEMBL — *which compound is this, and under which id?* and *what has
been measured for it?* — answered from a vendored, checksummed **subset** of ChEMBL release 37.
**The data is ChEMBL's, licensed CC BY-SA 3.0, and this server serves an adaptation of it**: a
selection of compounds, with activities aggregated into summaries. Every answer carries that fact as
a structured field. **Nothing here is a clinical, safety or efficacy claim**, and the answers say so
in their own payload as well as in these docs.

The decision to build it, and the owner's clearance of the legal question it had waited on, are
[`D-2026-10-23-chembl-is-vendored-only-as-share-alike-data-after-legal-review`](../../docs/decisions/D-2026-10-23-chembl-is-vendored-only-as-share-alike-data-after-legal-review.md).

## Tools

| Tool | The question | Reads |
| --- | --- | --- |
| `chembl_lookup_compound` | Which ChEMBL compound is this (id, name or structure)? | `compound`, `name` |
| `chembl_bioactivity_summary` | What does ChEMBL record as measured for it, per target, type and unit? | `activity_summary`, `target` |

Both are `read_only`: queries over a read-only file, with nothing to gate and nothing the plan gate
should hold, so a chemist can ask them while a plan is still being built.

- **Lookup is exact.** An id (case-insensitive), a name or synonym (whole, after case and whitespace
  folding) or a structure. A structure is matched on its InChIKey — the same key, else the same
  skeleton (stereochemistry, isotopes or protonation may differ), else the same for the largest
  fragment of a salt — and `match_kind` says which. A name that is not recorded is a miss, never the
  nearest spelling. A SMILES RDKit cannot read *in full* is refused (`"CCO junk"` would otherwise be
  searched as ethanol).
- **A summary is not a record.** A row aggregates the activities of one compound against one target
  with one standard type and unit: `n_exact_values`, `median`/`minimum`/`maximum` in the row's
  `units`, `n_censored_values` (`>`/`<`, counted and never averaged), `n_assays`, `median_pchembl`
  and the range of ChEMBL's assay confidence scores. Units are ChEMBL's standard units and rows of
  different units are never merged or converted. An identifier that matches several compounds is
  refused, naming them, rather than summarised as one.
- **A miss is not an absence.** The subset keeps a selection of ChEMBL. "Not in this subset" is the
  only thing a miss says; the tool docstrings tell the model so.

## The attribution every answer carries

Each result has `attribution` (`source` "ChEMBL release 37", `source_url`, `credit`, `licence`
"CC BY-SA 3.0", `licence_name`, `licence_url`, `changes` "subset + aggregation: …", `share_alike`,
`citation` (Mendez et al. 2019, DOI 10.1093/nar/gky1075), `data_tier`), a one-line `source` and a
`caveat`. ChEMBL asks for attribution, the release number and preserved ChEMBL ids
(`REQUIRED.ATTRIBUTION`); the licence asks for the credit, the licence name and a statement of changes.
`tests/test_attribution.py` holds the field for both tools in a hit, a miss and an empty filter, and
`tests/test_server.py` holds it on the wire. The two URLs come from the checksummed subset's `meta`
table, so the file that is distributed names its own source and this package's code names no host.

## What redistributing this image redistributes

**This is the first third-party copyleft corpus in the fleet.** The code beside it is MIT; the subset
is not.

- **ShareAlike.** `data/chembl_subset.sqlite` and its `dataset.json` are an *adaptation* of ChEMBL
  under CC BY-SA 3.0. A fleet image or a repository checkout that is *distributed* to anyone outside
  the legal entity that holds it distributes that file under the same licence, with the attribution
  above kept; the MIT licence of the code does not extend to it, and it does not extend to the code.
  Internal use is not distribution. Whether a long-lived store of answers (a result cache, notes that
  quote values) is itself an adaptation of the database is a question this README does not settle.
  Nothing derived from the subset should be written into a CC0 or MIT file of this repository.
- **The sui generis database right.** CC BY-SA **3.0 Unported** — the version ChEMBL uses — does not
  license the EU *sui generis* database right the way 4.0 does. In the EU, extracting or re-utilising
  a substantial part of a database can engage that right independently of copyright in individual
  facts, and the position then rests on the licence's general grant and national law. This is why
  the subset is a **selection and an aggregation** rather than a copy of the activity table, and why
  its size is bounded. It is not a determination that any use is or is not within the right.
- **Not legal advice.** The owner's statement that legal review is cleared is recorded in the
  decision above and was **not verified by the author** of this server. This README states what the
  licence text says and what the build does; it decides no legal question.

## The data

One SQLite file, `src/chemclaw_mcp_chembl/data/chembl_subset.sqlite`, beside a `dataset.json`
carrying the eight fields `mcp_server_kit` verifies (checksum on every start, a mismatch is a 503
naming both hashes). Built by [`scripts/chembl_extract_subset.py`](../../scripts/chembl_extract_subset.py)
from `chembl_37_sqlite.tar.gz`, which it refuses unless the SHA-256 equals the digest pinned in the
script **and** the one in EMBL-EBI's `checksums.txt`. Two tiers, one script:

| Tier | What it holds | Where it lives |
| --- | --- | --- |
| `fixture` | 13 named compounds (aspirin, imatinib and its mesylate, a solvent, an antibody, …) with every name, target and summary ChEMBL 37 has for them; 0.6 MB | **Committed** here: what the suite, a checkout and CI's image build run against |
| `full` | 301,199 compounds, 111,957 names and synonyms, 13,668 targets and 2,695,237 activity summaries; **271 MB** (SQLite, built in about 6 minutes from the 30 GB release database) | **Not committed**: built into the image by `servers/chembl/Containerfile` with `--build-arg CHEMBL_TIER=full`, which the `Jenkinsfile` passes for a release |

**Why the full tier is not committed.** Every other corpus in this fleet is measured in kilobytes
and the repository has no large-file mechanism; a subset of the size scoped for this server does
not belong in git history. The image build downloads the release at **build time** — the only
network contact — verifies it, writes the subset and discards the 30 GB of database; nothing of the
download reaches the runtime image, and no request-time call to EMBL-EBI exists in any tier. The
tier is in the file's `meta` table, in `dataset.json`'s version (`37-fixture` / `37-full`), on
`/healthz` (`datasets`) and in every answer's `attribution.data_tier`, so a fixture is never
mistaken for the full subset.

**Selection (full tier).** Compounds ChEMBL records at clinical phase 1 or beyond (`max_phase` ≥ 1),
plus compounds with at least three exact pChEMBL-bearing measurements in assays at confidence
score 9 (a direct single-protein target), plus the parent of any salt form selected. Names are
ChEMBL's preferred name and every recorded synonym.

**Aggregation.** Per compound, target, standard type and standard unit. Statistics are over
activities whose `standard_relation` is `=`; `>`/`<` values are counted in `n_censored_values`;
a value with no recorded relation is **not used** (ChEMBL leaves the relation empty on many
potency values; reading them as exact would be a guess, so they are left out and a group made only
of them has no row). Activities ChEMBL flags as a
potential duplicate or whose `data_validity_comment` is *Potential author error*, *Outside typical
range* or *Non standard unit type* are excluded from both. Medians are reproducible: the
imatinib/ABL1 IC50 row in `tests/test_tools.py` was reproduced with a plain `SELECT` over the
release.

**Refreshing.** `refresh_owner` is `team:chemclaw3-mcp-maintainers`, cadence `P6M`. A new release is
a reviewed edit of `RELEASE` and `SQLITE_SHA256` in the script, then a fixture rebuild
(`python -I scripts/chembl_extract_subset.py --tier fixture --workdir <dir> --out-dir
servers/chembl/src/chemclaw_mcp_chembl/data`), whose output is a data diff with a fresh `sha256`.
The output is deterministic: the same release and script give the same bytes.

**What it leaves out, deliberately.** Per-measurement records, assay descriptions and documents,
mechanism-of-action and drug-indication tables, bioassay ontology terms, molfiles, similarity
fingerprints and substructure search (`molfp` owns similarity search in Chemclaw3). A question the
summary cannot answer is answered in ChEMBL itself.

## Running it

```sh
make run-chembl                            # from the repository root; 127.0.0.1:8862
curl -s localhost:8862/healthz             # {"status":"ok","server":"chembl",…,"datasets":["chembl-subset@37-fixture"]}
```

The bearer token is `CHEMCLAW_CHEMBL_TOKEN`, enforced even on the loopback dev URL.

The image defaults to the **fixture** tier, so a bare `docker build` (and CI's image job, which
cannot move a 5.8 GB release) is cheap. A **release** image is the full subset, and must say so
(needs network to `ftp.ebi.ac.uk` and ~36 GB of free scratch for the build; `Jenkinsfile` passes it):

```sh
docker build -f servers/chembl/Containerfile --build-arg CHEMBL_TIER=full \
  --build-arg CHEMCLAW_REVISION=$(git rev-parse HEAD) -t chemclaw-mcp-chembl:full .
docker build -f servers/chembl/Containerfile -t chemclaw-mcp-chembl:fixture .   # what CI builds
```

A pod's tier is on `/healthz` (`datasets: ["chembl-subset@37-full"]`) and in every answer
(`attribution.data_tier`); a deployment that expected the full subset should check one of them.

## Operating it

Build, deploy, wiring and the fleet-wide variables are in
[`docs/operations.md`](../../docs/operations.md); what is particular to this server:

| | |
| --- | --- |
| Port / Service | 8862 / `chemclaw-mcp-chembl` |
| Token | `CHEMCLAW_CHEMBL_TOKEN` |
| Chemclaw3 | connector `chembl`, manifest installed from `chemclaw-contracts`, **off** until a deployment names it in `CHEMCLAW_CONNECTORS_ENABLED` (`default_enabled: false`) |
| Pod | requests 250m / 256Mi, limits 1 CPU / 512Mi; 2 → 4 replicas on CPU. The subset's pages are read through the page cache, which the memory limit counts but reclaims. |
| Own knobs | none. `MCP_MAX_SMILES_CHARS` / `MCP_MAX_MOLECULE_ATOMS` bound a structure; the result lists are capped by constants (`MAX_COMPOUNDS` 25, `MAX_ROWS` 200). There is deliberately **no** setting that moves the file: a swapped-in subset would be a different set of claims under the same attribution. |
| Readiness | `/healthz` verifies the subset's checksum and schema version, requires the attribution keys in its `meta`, and runs both tools on aspirin (`CHEMBL25`) by id, by name and as a summary. Any failure is a 503 naming the cause. |
| Admission | none — the argument is in `tests/test_fleet_manifests.py::CEILING_IS_ARGUED_ABSENT`: indexed reads of a read-only file, identifier and result lists bounded. |
