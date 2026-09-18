# D-2026-09-16-a-field-the-author-wrote-is-not-a-field-that-is-missing — A field the author wrote is not a field that is missing

**Status:** accepted · **Date:** 2026-09-16 · **Commit:** the dataset-manifest diagnostics. Extends
`D-2026-09-16-a-hand-rolled-model-cannot-see-a-key-it-was-not-told-about`, whose fix committed the
defect it had just described.

## What was measured

`mcp_server_kit/datasets.py`'s module docstring says the hand-rolled predecessor "actively misled on
the one file whose whole purpose is that a reviewer can audit it": a manifest written with
`"license"` parsed clean and then reported `licence` as *missing*, naming a field the author had
written.

`_explain` replaced it and bucketed every non-`extra_forbidden` error as absent. Driven at `6c6a0eb`:

```
{"version": 1, ...}     ->  missing required field(s) version
{"version": "  ", ...}  ->  missing required field(s) version
```

The first is a JSON number, on a line the author is looking at. The second is a blank a template
left behind — and `load_dataset`'s own `Raises:` said "absent or blank", two conditions with one
sentence between them.

The type case is also a **behaviour change** nothing recorded: the predecessor coerced with
`str(manifest.get(field, ""))`, so a numeric `version` or `sha256` parsed clean, and under pydantic's
lax mode `int` → `str` is refused.

## What was decided

**Keep the refusal, fix the message.** The coercion is not restored, and the argument is the
module's own "refuse rather than approximate" rule applied to provenance rather than to chemistry:

- JSON has no way to hold `1.10` as a number. It parses to `1.1`, so a corpus at version 1.10 would
  be recorded as a different version than the one a reviewer approved — silently, in the field that
  exists to tell two builds apart.
- `sha256` is worse. A digest that happens to be all digits loses its leading zeros, and one long
  enough reaches scientific notation.

The cost is a startup failure with a sentence naming the line, which is the loud direction. Measured
over the eight `dataset.json` files this fleet ships: every field of every one is already a JSON
string, so nothing shipped changes behaviour.

`_explain` now buckets on pydantic's own error types — `missing`, `value_error` (the blank
validator), anything else about a named field (wrong type, reported **with the type that was
found**), and `extra_forbidden` — and emits one sentence per bucket. The `Raises:` clause says four
cases because there are four.

## What keeps it true

- `packages/mcp_server_kit/tests/test_datasets.py::test_a_field_the_author_wrote_is_not_reported_as_missing`
  — the wrong-type and blank arms, parametrized over the model's own field list, pinning the refusal
  as well as the wording.
- `packages/mcp_server_kit/tests/test_datasets.py::test_every_provenance_field_is_required` — the
  absent arm, which needed a manifest with a key genuinely *removed* rather than emptied, and could
  not express one before.
