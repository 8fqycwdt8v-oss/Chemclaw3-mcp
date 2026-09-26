# D-2026-09-26-a-stand-in-refuses-what-its-consumer-refuses — A stand-in refuses what its consumer refuses

**Status:** accepted · **Date:** 2026-09-26 · **Supersedes:** the coercion bullet of
`D-2026-09-16-a-hand-rolled-model-cannot-see-a-key-it-was-not-told-about` ("A bare `tools:` key is
**coerced** to `[]` rather than refused"). The rest of that record stands: the models, the
`extra="forbid"` half, the derived `_REQUIRED`, the classification rule's home and the missing
`none` auth mode.

## The choice

`mcp_server_kit.testing`'s `HttpEndpoint` and `ConnectorManifest` stand in for the model Chemclaw3
reads a fleet manifest with. When the two disagree about a shape, one of them has to give, and there
were two options:

- **Keep the coercion.** A bare `tools:` means an empty list to the person who wrote it, and
  `assert_manifest_matches` has a better sentence for "declares `[]` while the server serves one"
  than a validation error does. That was the argument in 2026-09-16, and it is a real one about
  messages.
- **Refuse what the consumer refuses.** Taken. What a manifest means to its author is not the
  question this suite answers; whether the one reader that matters can load it is.

## What was measured

Driven against the consumer's own `ConnectorManifest.model_validate`, each of these fails at
Chemclaw3's startup with a `ConnectorError` naming the file, and each validated here:

| Shape | The consumer's refusal |
| --- | --- |
| an endpoint with no `transport:` | `union_tag_not_found` — the endpoint is a discriminated union there, and this model defaulted the tag to `http` |
| a bare `tools:`, `read_only:`, `state_changing:` or top-level list key | `list_type` — YAML's `None` is not a list, and this model coerced it to `[]` |
| `tools: []` | the classification validator — an endpoint serving nothing is not an endpoint |

A stand-in kinder than the model it stands in for turns this suite green on a manifest nobody can
load, which is the exact failure `D-2026-09-16-a-stand-in-that-refuses-a-real-field-is-not-a-stand-in`
names in the other direction. The two records together are the rule: **agree with the consumer in
both directions, and argue any difference by name.**

The message cost the 2026-09-16 record weighed is smaller than it looked. A bare `tools:` now fails
as a refusal naming the manifest and the field, not as a `TypeError` from a line in the helper — the
defect the coercion had been introduced to retire stays retired.

## The citation this retires

The 2026-09-16 record's `What keeps it true` names the test that held the coercion. That test's
behaviour was reversed rather than renamed, so `tests/test_decision_log.py` maps the retired name to
the test asserting the reversal **and to this record**, and refuses the mapping unless this file
exists — so a citation can be retired only beside the record that supersedes it. The same record's
citation of `test_a_manifest_with_a_null_tools_key_is_named` still resolves; its body now asserts a
refusal rather than the "declares `[]`" message the 2026-09-16 bullet described.

## What keeps it true

- `packages/mcp_server_kit/tests/test_manifest_model.py::test_an_endpoint_the_consumer_cannot_load_is_refused_here`
  — the three shapes, each refused here as it is refused there.
- `packages/mcp_server_kit/tests/test_manifest_model.py::test_a_bare_top_level_list_key_is_refused_as_the_consumer_refuses_it`
  — the top-level half of the bare-key case.
- `packages/mcp_server_kit/tests/test_datasets.py::test_a_manifest_with_a_null_tools_key_is_named`
  — a bare `tools:` is a refusal naming the manifest, not a type error.
- `tests/test_consumer_agreement.py::test_the_stand_in_manifest_model_agrees_with_the_model_that_reads_a_manifest`
  — the same shapes probed against the consumer's model itself, where a checkout is present.
- `tests/test_decision_log.py::test_every_test_a_record_names_still_exists` — a retired citation
  must name its replacement and this record.
