# D-2026-09-26-a-settings-shape-the-bound-scan-cannot-read-fails-the-suite — The bound scan follows inherited prefixes, nested models and aliases, and fails on what it cannot read

**Status:** accepted · **Date:** 2026-09-26

## What was found

`tests/test_fleet.py::numeric_env_bounds` derives every environment variable first-party code turns
into a number, and the deployment ratchet refuses a shipped file that moves one. Its settings half
read one class body at a time, so three shapes measured on 2026-09-12 were outside it: a settings
class inheriting `env_prefix` from a parent (the field derived without the prefix), a nested
`BaseModel` field (skipped as "not numeric"), and `Field(validation_alias="REAL_NAME")` — derived
under the prefixed field name, a variable pydantic-settings does not read, so the ratchet would
refuse the harmless name and wave the real one through. None exists in `src/` today.

## What was decided

The row asked whether following them is worth the AST, or whether the floor (`_BOUND_ANCHORS`) plus
the row is the honest arrangement. **Follow all three, and fail the suite on any form of them the
scan cannot read**, because the floor catches a derivation that *shrinks* and cannot catch one that
returns a wrong name — which is what the alias shape does.

- The derivation reads the whole tree at once, resolves a base class by name (same module first,
  then a unique match anywhere) and merges `env_prefix`, `env_nested_delimiter` and
  `case_sensitive` down the hierarchy as pydantic does.
- A nested first-party model with a number in it contributes its field's JSON variable, and one
  variable per nested field under `env_nested_delimiter`.
- An alias contributes the alias as written; `AliasChoices` of literals contributes each.
- An `AliasPath`, a computed alias or prefix, an unpacked `model_config`, and a parent name two
  first-party classes share each raise with the location, rather than derive a guess.

The one shape still outside the scan is a read through a helper it does not know by name;
`_BOUND_HELPERS` and `_BOUND_ANCHORS` carry that, as before.

## What keeps it true

- `tests/test_fleet.py::test_the_derivation_follows_inheritance_nesting_and_aliases`
- `tests/test_fleet.py::test_a_settings_shape_the_derivation_cannot_read_fails_loudly`
- `tests/test_fleet.py::test_an_ambiguous_parent_fails_loudly_rather_than_picking_a_prefix`
- `tests/test_fleet.py::test_no_shipped_deployment_moves_a_bound_the_code_reads_from_the_environment`
