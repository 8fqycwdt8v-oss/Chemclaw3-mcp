# `safety` — cited hazard, genotoxicity and ICH impurity tables

Three questions a chemist asks separately, kept separate, each answered from a committed table with
a citation on it. **Nothing here is a clearance, a classification, or a risk assessment**, and every
result says so in its own payload rather than only in these docs.

## It replaces a Chemclaw3 bundle rather than adding a second one

Chemclaw3 ships its own in-tree `safety` connector, and `CLAUDE.md`'s exclusion table forbids a
second answer to one question. This is a **port**, not a duplicate: same manifest `name`, same three
tools, same argument names, same docstrings — the model-facing prose is carried over word for word,
because every disclaimer in it exists to prevent a mistake that was measured in a live run (an
invented ICH M7 class and purge factor, a palladium PDE recited from training, "no hazards detected"
told six times to a chemist about to sign a risk assessment).

The two cannot both answer, and that is enforced by Chemclaw3's own mechanism rather than by
convention:

- Bundles are addressed by name, so `CHEMCLAW_CONNECTOR_URLS` has one `safety` key.
- `CHEMCLAW_CONNECTORS_DIR` is a `PATH`-style list and **the first directory wins a name collision**
  (`connectors/registry.py::_bundle_dirs`, "first dir wins"). Putting this fleet's `manifests/` ahead
  of Chemclaw3's own connectors directory is what makes this server the `safety` the agent sees.

**One thing does not come with the port, and it is not an oversight.** Chemclaw3's bundle ships
`skills/safety-screening/SKILL.md` — the *judgment* about which of these three tools answers which
question, and how to report what comes back. A skill is architecture layer 3 in that repository and
this fleet has no equivalent seam, so the SKILL.md stays there and this manifest declares no
`skills:`. Keep it reachable when wiring this server up: the tools are deterministic and have no
opinion, and the judgment is the half that lives in the skill.

## Tools

| Tool | The question | Reads |
| --- | --- | --- |
| `screen_hazards` | Is this safe to run today? | 11 structural SMARTS motifs plus 5 pairwise incompatibilities checked across a reaction's components |
| `screen_genotoxic_alerts` | Will this need a mutagenic-impurity control strategy? | 9 DNA-reactive structural alerts plus the nitrosamine formation route |
| `ich_impurity_limit` | What is the number? | ICH Q3C residual-solvent classes and limits; ICH Q3D elemental-impurity PDEs |

All three are `read_only`, and they must stay open under an unapproved plan for a reason beyond
convenience: a hazard screen, an alert list and a published limit are exactly the checks a chemist
wants *before* deciding whether to approve the work.

**The three answer different questions and one must never be reported as another.** Nitrobenzene is
the case that proves the split is real: an ordinary reagent the hazard table is right to pass and the
alert table is right to flag. Merging them is how a hazard screen gets reported as an ICH M7
assessment.

### What an empty result means

`screen_hazards` returning no flags means *no rule in the table matched*. It says nothing about
toxicity, exposure, thermal stability, scale, or the process around the reaction. `verdict` renders
this as "No rule in the hazard table matched. This is not a safety assessment." and deliberately
never uses a word resembling "safe" — an over-trusted screen is more dangerous than no screen,
because it converts an absence of knowledge into apparent assurance.

`screen_genotoxic_alerts` returning no alerts is nine patterns not matching, **not** a negative
mutagenicity prediction. This system has no (Q)SAR pair, no Ames corpus and no expert rule base, so
it cannot produce an ICH M7 class, an acceptable intake or a purge factor.

`ich_impurity_limit` returning `limit: null` means *these tables do not carry the substance*, not
that no limit exists. Nickel, `tert`-butyl alcohol, silver and gold all have real guideline entries
and are deliberately absent — see below.

Each of those sentences ships **inside the result** as a pydantic `computed_field`, not merely in a
docstring. A plain property is dropped by serialization, and Chemclaw3 measured the consequence: the
disclaimer existed in the code, passed every unit test, and never reached the model writing the
answer. `tests/test_server.py` asserts all three on the wire.

## Running it

```sh
make run-safety                            # from the repository root; 127.0.0.1:8859
curl -s localhost:8859/healthz             # {"status":"ok","server":"safety"}
```

The bearer token is `CHEMCLAW_SAFETY_TOKEN`, and the same variable name is read on both sides.
Chemclaw3's in-tree bundle declares `auth: {mode: none}` because it was only ever dialled over
loopback from the same pod; a server in another image is dialled across a network, so this one
declares bearer and enforces it even on the loopback dev URL.

`CHEMCLAW_SAFETY_MAX_COMPONENTS` (default 64) bounds a component list. Chemclaw3 carries the same
knob as `settings.safety_max_components`, and the number is not arbitrary: both screens check their
pair rules as a cross-product, so 13 KiB of SMILES was measured producing 251,000 flags and blocking
a serving connector's event loop for 2.48 s. An oversized list is **refused, never truncated** — a
screen that silently dropped components would report "no rule matched" for chemistry it never looked
at.

`safety_rules_path` has **no** counterpart here, deliberately. Chemclaw3 let a site point at its own
rule table; here the table is a vendored corpus with a checksum, because a swapped-in table would be
a different set of claims wearing the same citations. Extending it is a pull request.

## The data

Four corpora, each with a `dataset.json` carrying its licence, checksum and provenance, plus a fifth
that is a copy of `chem`'s reagent table (see below).

| Corpus | What it is | Provenance |
| --- | --- | --- |
| `data/rules/rules.yaml` | 11 structural hazard motifs + 5 incompatible pairs | First-party SMARTS written against Bretherick's 8th ed., Bräse 2005, Green 2020, Buckley 2021, Peet & Weber 1988 |
| `data/genotox/genotox_alerts.yaml` | 9 DNA-reactive alerts + 1 formation pair | Ashby & Tennant 1988, Benigni & Bossa 2011, ICH M7(R2) 2023, EMA 2020 |
| `data/ich_q3c/ich_q3c.yaml` | 62 residual solvents, classes and limits | ICH Q3C(R9), Tables 1/2/3 |
| `data/ich_q3d/ich_q3d.yaml` | 21 elements, three route PDEs each | ICH Q3D(R2), Appendix 2 Table A.2.1 |

**Refreshed by:** whoever changes one, in a pull request. There is no ingestion job. The ICH tables
change when a guideline revision does, and that is a person reading the source document and editing
a row — never a fetch.

### `rules.yaml` is first-party, and the manifest says why rather than naming a licence

It shipped as `UNRESOLVED` and is settled: **CC0-1.0**, on the same basis as `genotox`.

The file cites a source on every rule and reproduces none of them. Bretherick's Handbook and the
four cited papers are prose — they contain no SMARTS — and the patterns here were written *and
debugged* in Chemclaw3, which the `peroxide` rule shows plainly: it carries an in-repo fix for
`[OX1-]`, added because the two-coordinate-only pattern had screened sodium peroxide clean. The
underlying facts, that an organic azide is shock-sensitive, are not copyrightable subject matter.
So the SMARTS and the flag text are this project's own expression of published science that each
row attributes.

**The three corpora here answer one provenance question three ways, and that is deliberate.**
`genotox` and `rules` are first-party expression → CC0-1.0. `ich_q3c` and `ich_q3d` *transcribe
figures* out of a guideline and therefore carry ICH's own reproduction terms instead. Leaving
`rules` open while `genotox` — identical in provenance — was resolved was the actual defect: to a
later maintainer it reads as a real problem with this file rather than as caution.

`tests/test_dataset.py` asserts the *reasoning* rather than the identifier. A bare `CC0-1.0` is
precisely what the original guard existed to stop somebody typing to satisfy the loader, and that
failure mode does not disappear now that the identifier happens to be correct.

### The deliberate omissions, which must survive

A transcription's honest half is what it leaves out. Each of these returns a **miss** that says "this
system does not carry the number — read it from the guideline", and filling one in from memory would
reintroduce precisely the fabrication these tables replaced:

- **Q3C:** `tert`-butyl alcohol (Q3C(R8) assigns it a PDE the transcriber could not verify). Water is
  absent because Q3C does not cover it. The ambiguous abbreviations `EDC`, `DMA` and `TCE` resolve to
  nothing, because a confidently wrong row arrives with a real citation attached.
- **Q3D:** silver, gold and nickel (Q3D(R2) revised all three and the revised numbers could not be
  verified); the cutaneous and transcutaneous routes; and the "other elements" of §4, for which the
  guideline establishes no PDE at all.
- **Q3C's revision label is the one field nobody has checked against the source.** An adversarial
  review verified all 62 values and could not verify "R9 / 2024" — which appears in every row's
  citation, so if it is wrong the whole file cites the wrong document while every number in it is
  right. Checking it is a one-line change that touches no figure.

`tests/test_dataset.py` asserts each omission is still omitted, and each caveat still present in the
manifest a reviewer reads.

### The corpus this server carries twice

`data/reagents/` is a **byte-identical copy** of `servers/chem/src/chemclaw_mcp_chem/data/` — the
same 61-reagent table, the same checksum, the same `dataset.json`. `ich.py` needs it for one thing:
a SMILES is not a spelling of a name, so no amount of synonyms in the guideline files lets
`ich_impurity_limit("C1CCOC1")` reach the tetrahydrofuran row, and `THF`/`2-MeTHF` reach it the same
way.

One server never imports another — that rule is what keeps a dependency closure a dependency
closure — so a table two servers need is carried by both. What makes it safe rather than merely
accepted is that it is *provably* the same file: `tests/test_dataset.py` and `tests/test_fleet.py`
both assert byte-identity, from inside and outside. If the two ever have to diverge, that is a
decision with an argument behind it, and those tests are where the argument gets written down.

The same applies to `engine/chem.py`, which is a third copy of Chemclaw3's canonical-SMILES
definition (`chem` holds the second). `tests/test_canonicalization_contract.py` carries the same
table of literal strings all three must produce, derived by running Chemclaw3's own function.

## What was left behind in the port

- **`science/safety/notes.py`** and its ~370 lines of tests. It extracted structures from
  knowledge-graph notes for Chemclaw3's `kg-validate` hazard gate — the check that makes an
  agent-authored procedure document its flags before a pull request merges. That gate is a property
  of a knowledge graph in a git repository, and this repository has neither. A server answers
  questions; it does not gate a pull request.
- **`at_least(severity, threshold)`.** Its two callers were that gate and the agent-side tool. With
  the gate gone it has none, and a helper with no caller is the shape of a control that is claimed
  rather than enforced.
- **`molecular_weight`** and the density index, from the two vendored modules. `chem`'s charge table
  needs both; nothing among these three tools asks either question.
