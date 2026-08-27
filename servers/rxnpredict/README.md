# `rxnpredict` — forward reaction and reaction-condition prediction

What will this reaction give, and how would people run it? Several open-source models answer both
questions independently and their ranked outputs are combined by Borda-weighted voting, gated on a
coarse reaction class. The spread between the models comes back with the consensus, because "four
of five agree" and "only the rule-based one produced this" are different answers.

## Provenance

**Forked from [`8fqycwdt8v-oss/chemclaw2_forward`](https://github.com/8fqycwdt8v-oss/chemclaw2_forward)**
(branch `claude/reaction-condition-meta-model-29YIz`, MIT, same owner), and adapted to this fleet's
standards. The aggregator, the classifier, the trust-prior machinery, the preprocessing and every
predictor adapter are upstream's work; upstream's own tests for all of them pass here unmodified,
which is the evidence that the fork is the same model rather than a similar one.

### What changed, and why

| Change | Reason |
| --- | --- |
| **The Claude predictor is deleted** — both adapters, `llm_prompts.py`, the `[claude]` extra, the `anthropic_*` settings and its trust prior | It called the Anthropic API *per request*, and was live by default: `enabled_forward_models` defaults to `*`, so installing the extra with a key present was enough. No server in this fleet reaches a third-party API, and leaving the code behind a config flag is the arrangement that has failed before |
| **Bearer auth is ASGI middleware, not a route dependency** | Upstream mounted `fastapi-mcp` and applied `Depends(require_token)` to its routers. A mount bypasses the enclosing app's dependencies, so the credential guarded the REST surface and not `/mcp` — the surface that matters. `tests/test_server.py` asserts the 401 |
| **`diskcache` → an in-process bounded LRU** | A rootless read-only container cannot write `~/.cache`. The cache now needs no volume, no TTL and no clear-cache tool; it does not survive a restart, which is a fair trade for a memo of a deterministic function |
| **Trust priors are a vendored dataset** | They were written to a home directory by a script and loaded on startup — numbers that change every ranking, with no licence, no checksum and no record of which run produced them |
| **`clear_prediction_cache` and `health_check` are not tools** | `/healthz` comes from the transport; clearing a cache is an operator action. Dropping both leaves this server with no state-changing surface at all |
| **Env vars are prefixed `CHEMCLAW_RXNPREDICT_`** | Upstream read bare names (`DEVICE`, `ENABLED_FORWARD_MODELS`), which collide the moment several servers share a deployment's environment |
| **Checkpoint download URLs moved out of the code** into this file | The no-egress scan refuses a remote host literal in a module — see below |

## Tools

| Tool | Answers |
| --- | --- |
| `predict_forward_reaction` | What products will these reactants give? (consensus of every enabled model) |
| `predict_reaction_conditions` | Catalyst, solvent, reagent and temperature for a known transformation |
| `predict_forward_single_model` | The same, from one named model — for interrogating a predictor |
| `predict_conditions_single_model` | The condition-side counterpart |
| `list_available_models` | Which predictors this deployment serves, which it does not, and why |
| `classify_reaction` | The coarse SMARTS reaction class used for gating |

All six are `read_only`.

## Running it

```sh
CHEMCLAW_RXNPREDICT_TOKEN=dev-token \
  uv run uvicorn chemclaw_mcp_rxnpredict.app:app --host 127.0.0.1 --port 8857
```

With no predictor extras installed the server starts, serves its tools, and reports every predictor
as unavailable with the reason — which is the honest behaviour and is what `list_available_models`
is for. The same tool reports a predictor that *did* load but is switched off by
`CHEMCLAW_RXNPREDICT_DISABLED_MODELS` or the `ENABLED_*_MODELS` allow-lists as unavailable too, and
the single-model tools refuse it: one function decides what this deployment serves, so a control an
operator applied cannot be walked around through a different tool. For a working tool surface with no model weights at all, enable a deterministic double:

```sh
CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS=fake_a  # see engine/base_doubles.py
```

## The predictors, and which ones this image carries

The shipped image installs **`reaction_t5_v2`** and **`rxn_insight`** — upstream's Phase A, and the
only pair that co-installs. `molecular_transformer` and `reagents_mt` pin legacy PyTorch through
OpenNMT-py and cannot share an environment with `reaction_t5`; the rest need checkpoints that a
person has to fetch. Their adapters ship anyway: each registers only if its import succeeds, so an
uninstalled predictor is a row in `list_available_models` with a reason rather than an absence.

Where a checkpoint comes from, for whoever builds an image with one — these live here rather than in
the adapters because the no-egress scan refuses a remote host in a module, and because "where a
human obtained this file" is exactly what a README is for:

| Predictor | Checkpoint | Env var |
| --- | --- | --- |
| `reaction_t5_v2` | `sagawa/ReactionT5v2-forward` on HuggingFace, pinned in `scripts/fetch_models.py` | `HF_HOME` |
| `molecular_transformer` | `MIT_mixed_augm_model_average.pt`, from the `pschwllr/MolecularTransformer` releases | `MOLECULAR_TRANSFORMER_MODEL_PATH` |
| `megan` | the `molecule-one/megan` repository and its release checkpoint | `MEGAN_MODEL_PATH` |
| `t5chem` | the `t5chem` PyPI package's published weights | `T5CHEM_MODEL_PATH` |
| `chemformer` | a fine-tuned Chemformer checkpoint (`MolecularAI/MolBART`) | `CHEMFORMER_MODEL_PATH` |
| `parrot` | `wangxr0526/Parrot`, cloned with its checkpoint | `PARROT_MODEL_PATH` |
| `reagents_mt` | `Academich/reagents` release checkpoint | `REAGENTS_MT_MODEL_PATH` |
| `askcos_condition` | `askcos-core` from MIT's distribution | — |

## Offline, and how that is made true

The image bakes its weights in at build time (`scripts/fetch_models.py`, run in a builder stage that
is thrown away) and the runtime sets `HF_HOME`, `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`.
Those two switches matter beyond the download: without them the HuggingFace libraries still contact
the hub for a metadata check on a model they already have, which is a network call an otherwise
"offline" server would keep making.

Behind them sit the fleet's usual layers — the runtime egress guard, the AST scan over this
package, and `deploy/networkpolicy.yaml` with an empty `egress:`. Together they mean that a build
which failed to bake a checkpoint fails loudly on first inference rather than quietly downloading a
model nobody reviewed.

## Data

`data/trust_priors.json` — per-reaction-class weights for each predictor's votes, with
`dataset.json` beside it carrying the licence and checksum. **Shipped empty**, deliberately: no
calibration run has been performed against this fork's predictor set, and inventing per-class
weights would be fabricating the numbers that decide every ranking. With an empty table the
aggregator falls back to the global per-model priors in `Settings.model_trust_priors`, which are the
figures each predictor's own published benchmark reports.

To produce a real one, run `scripts/calibrate_rxnpredict_priors.py` (repository root) against a
labelled reaction set, review the diff, and bump the dataset version.

## What it is not

A prediction, not a result. These models are trained largely on USPTO patent reactions: strong on
common couplings, weak on stereochemistry, weak on rare classes, and entirely unaware of your
substrate's other functionality, your scale, or the hazards of what they suggest. An ensemble makes
the ranking more robust; it does not make the answer a measurement. Check any suggested solvent
against its ICH class and hazard data — the `props` server answers that — before it reaches a plan.
