# `rxnlabel` — what a reaction is made of, and what it is called

Two capabilities, and they are the two halves of turning a reaction SMILES into something a
database can be asked facet questions of:

| Tool | Answers |
| --- | --- |
| `represent_reaction` / `represent_reactions` | The atom-mapped reaction, and for every species its canonical form, Bemis-Murcko scaffold, functional groups, and **role** — starting material, product, reagent, solvent, catalyst, ligand, base, additive |
| `name_reaction` / `name_reactions` | The named reaction and its class, from 527 curated SMIRKS |
| `labeller_version` | What this deployment's labels are stamped with — ask before storing any |

## Why the role assignment is the interesting half

A recorded reaction says `reagent` for a phosphine and for a carbonate alike. The five values an
ELN column or a patent extractor uses — reactant, reagent, solvent, catalyst, product — contain
neither "ligand" nor "base", and those two are most of what a chemist actually asks for. Deciding
them is this server's job, and it uses three sources of evidence in order of how much each knows:

1. **The slot the species was written in.** `reactants>agents>products` already separates products
   from everything else, and it is the one fact the extractor was certain about.
2. **The atom map.** A species on the left that contributes no atoms to the product did not become
   the product — it is a reagent, a base, an oxidant. This is the reactant-versus-reagent split of
   Schneider, Lowe, Sayle and Landrum's *What's What* (JCIM 2016), computed from a map rather than
   from heuristics because a map is better evidence.
3. **Structure rules**, which turn "not a substrate" into *which* kind: catalyst, ligand, base,
   solvent, additive.

Two of those rules are context-dependent, which is why the unit of work is a reaction and not a
molecule: **triphenylphosphine is a ligand in a Suzuki and a stoichiometric reagent in a Mitsunobu**,
and only the rest of the flask distinguishes them.

Rules and a dictionary, not a learned classifier — deliberately. A misclassified ligand is not a
wrong number, it is a wrong *count* in a frequency table somebody then quotes, and a rule that says
"phosphorus with three carbon substituents, in a reaction that also contains a transition metal"
can be read, argued with and corrected. A softmax cannot.

## The models, and what happens without them

| Component | Licence | Optional? |
| --- | --- | --- |
| RDKit | BSD | no — canonicalisation, SMARTS, scaffolds |
| [RXNMapper](https://github.com/rxn4chemistry/rxnmapper) (Schwaller et al., *Sci. Adv.* 2021) | MIT | yes, `models` extra |
| [Rxn-INSIGHT](https://github.com/mrodobbe/Rxn-INSIGHT) (Dobbelaere et al., *J. Cheminform.* 2024) | MIT | yes, `models` extra |

The image installs both; a developer's checkout does not, because RXNMapper drags torch behind it
and installing gigabytes to run a SMARTS test is a bad trade.

**Absence degrades and says so.** Without the mapper, reactants and reagents are separated by the
slot they were written in rather than by an atom map — coarser, and honest. Without the classifier,
reactions are labelled without a name. What makes that safe rather than a silent quality gradient
is `labeller_version`: it names which components were present, so a caller's rows carry a different
version and go stale the moment a deployment installs one. **The corpus repairs itself.**

**"Optional" is about absence, not about breakage, and `/healthz` is where the two are separated.**
A distribution that is not installed is this deployment's decision and the pod is ready. A
distribution that *is* installed and will not construct — a checkpoint that did not mount — is a
broken image, and the pod is not ready: it would otherwise take traffic and write coarse labels
stamped `mapper@absent`, permanently indistinguishable from the rows a deployment that never wanted
a mapper produced on purpose. `engine/readiness.py` makes exactly that comparison, and also labels a
fixture reaction end to end so that a broken RDKit fails the probe rather than every call. Which
components a pod actually has is `labeller_version`'s answer, on the authenticated surface where a
caller needs it to decide whether a stored label is stale.

**Construction was the only thing that comparison could see, and breakage is the larger half.** A
namer that *imports* and raises on every reaction reported present, answered 200, and labelled a
corpus "nothing matched" — which is also the commonest correct answer this server gives. The probe
now runs both components on the fixture, and a reaction they raised on comes back with `degraded`
naming them and a version stamped `mapper@failed` / `namer@failed` — a third word, distinct from
`absent` and from a version number, so the row is stale against a healthy pod instead of equalling
one. Each such answer is counted on `chemclaw_mcp_degraded_total`; see
`D-2026-09-12-a-degradation-that-is-not-counted-is-a-degradation-nobody-sees`. An out-of-memory is
counted and reported the same way but is **not** a reason to take the pod out, because readiness and
liveness share `/healthz` here and a transient refusal would restart it into the same pressure.

## What this server deliberately does not answer

- **`rxno_id` is always null.** Rxn-INSIGHT names reactions in its own vocabulary and carries no
  ontology id; mapping one to the other is an unaudited lookup table, and a wrong `rxno_id` is
  worse than none — the id is what a caller uses to escape the three-vocabularies problem in the
  first place. A corpus that ships its own keeps it.
- **`confidence` is always null.** A SMIRKS either matched or it did not.
- **`OtherReaction` is reported as no name.** A frequency table whose largest row means "we do not
  know" is a worse answer than one whose coverage sentence says so properly.
- **Nothing is cached.** The caller's row *is* the cache: a label is stored against a reaction and
  re-derived only when the version moves. A cache in front of that would answer from a superseded
  labeller while the row said it was stale.

## The functional-group vocabulary is a wire contract

`species.py`'s list is first-party and used **even where Rxn-INSIGHT is installed**. The names are
stored in the caller's `reaction_species.functional_groups` and queried by exact array containment
(`@> ARRAY['aryl halide']`), so taking them from an optional dependency would mean a corpus labelled
with the extra installed and one labelled without it answer the same query differently. Renaming one
is a `SERVER_VERSION` bump.

## What bounds this pod

Two bounds, and they are not the same bound.

| Bound | Default | How |
| --- | --- | --- |
| Reactions in one request | 500 | `CHEMCLAW_RXNLABEL_MAX_BATCH`; refused with a message saying how much to ask for |
| Labelling in flight | 2 slots | `CHEMCLAW_RXNLABEL_MAX_CONCURRENT_BATCHES`; refused, never queued |

**A slot is a core, not a call.** The pod is limited to two cores, so the ceiling is two — and what
a call is *charged* is `mapping.inference_threads()`: one for the RDKit path, whatever the mapper's
transformer is configured to spend where RXNMapper is installed. Charging one per call would
under-count by torch's intra-op width, which torch takes from the machine's physical cores rather
than from the container's cgroup, so on a large node one mapped batch could be sixty-four runnable
threads inside a two-core pod.

Measured on the RDKit-only path: **2.8 ms per reaction**, flat from a batch of 10 to a batch of 500,
with wall clock equal to CPU time — SMARTS matching holds the GIL, so 1, 2 and 4 threads labelling
50 reactions each gave 581, 425 and 418 reactions/s. Concurrency there buys nothing; the ceiling is
what keeps a burst from making every batch slower than it would have been alone.

**Refused, never queued.** A batch held behind another comes back after `connector.yaml`'s
`request_timeout` has expired — an answer nobody is waiting for, computed at the expense of one
somebody is. The drain's right response to a refusal is to re-send the identical batch.
`engine/admission.py` has the argument.

## Running it

```
uv run uvicorn chemclaw_mcp_rxnlabel.app:app --host 127.0.0.1 --port 8865
```

`CHEMCLAW_RXNLABEL_TOKEN` is enforced; unset, every `/mcp` request is refused with 401.

## Chemclaw3 reaches this by configuration, not by discovery

**Its manifest is not in `manifests/`, and that is deliberate.** These are internal primitives for a
background corpus-labelling drain; mounting them on `CHEMCLAW_CONNECTORS_DIR` would put
`represent_reactions` into the agent's prompt as a tool to choose between, and nothing in a
conversation should be picking it. Chemclaw3 addresses this server through `rxnlabel_server_url` and
`rxnlabel_server_token_env` — the same call `core/config/calculators.py` makes for the calculation
server.

The registration lives in [`manifests-internal/`](../../manifests-internal/), which no published
`export` line names, and `connector.yaml` declares `mount: backend` — a key Chemclaw3's
`extra="forbid"` manifest model refuses, so pointing a path there anyway is a startup error naming
the file rather than a tool surface that quietly grew.
