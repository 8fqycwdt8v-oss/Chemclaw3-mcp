# D-2026-09-16-a-stand-in-that-refuses-a-real-field-is-not-a-stand-in — A stand-in that refuses a real field is not a stand-in

**Status:** accepted · **Date:** 2026-09-16 · **Commit:** the manifest-model reconciliation. Extends
`D-2026-09-14-the-gate-that-catches-a-change-is-the-gate-of-the-tree-it-is-made-in`, which is the
argument `mcp_server_kit.testing.ConnectorManifest` exists on, by checking the claim it rests on.

## The claim, and what it was worth

`HttpEndpoint`'s docstring: modelled "the way the repository that reads it models it… so the refusal
happens here instead" of aborting `Chemclaw3`'s startup. Nothing checked it. Read against
`chemclaw/connectors/manifest.py` and measured at `6c6a0eb`, it was wrong in **both** directions at
once — which is the worst available combination, because each direction hides the other from a
reader who tries one probe.

**False accepts** — validated here, aborts startup there:

```
{"name": "Calc_Server!", "description": "x" * 20_000}
```

The consumer enforces `pattern=r"^[a-z][a-z0-9-]*$"` on `name` and `max_length=4000` on
`description`. This model enforced `min_length=1` on each and nothing else. So the startup abort the
model exists to pre-empt still got through.

**False refuses** — real fields of the consumer's model, refused here with "is not a connector
manifest": `endpoint.knowledge_read`, and the top-level `jobs`, `skills`, `profiles`, `note_types`
and `relations`. Latent, because no shipped manifest declares one — which is exactly why it went
unnoticed. A latent false refusal costs nothing until the day somebody writes the field and is told
their manifest is not a manifest.

## What changed

The model now enforces the two constraints it was missing, as named constants
(`CONNECTOR_NAME_PATTERN`, `MAX_MANIFEST_TEXT_CHARS`) held against the consumer's real values, and
declares the six fields it was refusing.

The five top-level lists are accepted and **not** validated in depth. `JobSpec` alone carries an
effect model, a queue, a compensation and three cross-field validators; a second copy of it here
would be a second answer to one question, which is the defect this model exists to avoid one layer
down. What closes that gap instead is running the manifests this fleet actually publishes through
the consumer's **own** model.

Four differences remain and each is argued in the class docstring rather than left to be found:

| key | here | there | why |
| --- | --- | --- | --- |
| `mount` | accepted | refused | that asymmetry *is* `manifests-internal/` |
| `auth.mode` | `bearer` only | `bearer` or `none` | `CLAUDE.md`'s rule for every manifest, loopback included |
| `endpoint` | required | optional | a server here that serves no MCP surface is not a server |
| the `read_only`/`state_changing` partition | not enforced | enforced | held here by `assert_manifest_matches` against the tools a server **actually serves**, which is the stronger question |

The fourth was found by the probe table rather than by reading, and is the reason the table is in
the tree rather than in this record.

## How it is checked

`tests/test_consumer_agreement.py` already resolves a `Chemclaw3` checkout and already runs that
repository's own suite against this tree. The new check reuses both: a table of probe documents,
each with the verdict expected on **each** side, plus every manifest under `manifests/` (expected
accepted) and `manifests-internal/` (expected refused, which is the whole mechanism behind that
directory). The documents are handed to the consumer's interpreter and its model returns a verdict
per row.

Half of it runs everywhere: the verdicts *here* need no checkout, so a loosened stand-in is red on a
laptop. The other half skips with the reason when there is no checkout, and a skip is counted and
named by `tests/conftest.py` — it is not a pass.

## What keeps it true

- `tests/test_consumer_agreement.py::test_the_stand_in_manifest_model_refuses_what_it_says_it_refuses`
  — the half that needs nothing but this repository.
- `tests/test_consumer_agreement.py::test_the_stand_in_manifest_model_agrees_with_the_model_that_reads_a_manifest`
  — every probe and every shipped manifest through the consumer's own model, plus the two literals
  this repository copies from it.
- `tests/test_consumer_agreement.py::test_every_place_the_live_lanes_look_for_the_checkout_is_looked_in_here`
  — the lookup both halves depend on, unchanged.
