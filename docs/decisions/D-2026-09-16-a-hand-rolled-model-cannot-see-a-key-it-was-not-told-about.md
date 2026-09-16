# D-2026-09-16-a-hand-rolled-model-cannot-see-a-key-it-was-not-told-about — A hand-rolled model cannot see a key it was not told about

**Status:** accepted · **Date:** 2026-09-16 · **Commit:** the two `mcp_server_kit` declarations that
are now pydantic models. Supersedes nothing; it removes two hand-written validators from a package
whose runtime dependencies already include pydantic.

## The two places

`mcp_server_kit` validates two declaration files, and validated both by hand:

- **`datasets.py`** — a `_REQUIRED` tuple, a `missing = [...]` comprehension, an `isinstance(parsed,
  dict)` check and six `str(manifest[...])` coercions.
- **`testing.py`** — `load_manifest` returning a raw `dict[str, Any]` that two call sites then walked
  defensively, one of them with a comment explaining that a bare `tools:` key parses to `None` so
  `endpoint.get("tools") or []` was load-bearing.

Neither was wrong about what it checked. Both were models, written twice, in a package that ships
`pydantic>=2.9` as a runtime dependency — so the cost of the model was already paid and only the
capability was missing.

## What the hand-rolled version could not do, and what it cost

**A required-field loop reads the keys it knows and is silent about the keys it does not.** On a
`dataset.json` that is the whole point of the file: it exists so that a reviewer can audit a
corpus's provenance a year later. A manifest written with `"license"` — the spelling most of the
English-speaking world uses — parsed clean, and the loader then reported **`licence` as missing**.
The error named a field the author *had* written, under a spelling they had not, and a reviewer
reading that message looks at the file, sees a licence, and concludes the loader is broken.

`extra="forbid"` reports both halves, and `_explain` puts them in one sentence: the key that was
written, and the field that therefore reads as absent.

**It also found three manifests carrying keys nothing reads.** `chem`'s reagent table, `safety`'s
copy of it and `props`' solvent sheet each declared `text_column` and `smiles_column`. Neither
repository has ever read either — checked by `grep` across both trees — and they were invisible
precisely because the loader took the six keys it knew and said nothing about the rest, so a
reviewer seeing them had every reason to believe something consumed them. They are deleted. (The
two identical copies stayed byte-identical, which is what
`test_the_reagent_table_two_servers_carry_is_one_file` requires.)

**On the manifest side the missing capability is the same and the consequence is somebody else's
startup.** Chemclaw3's `HttpEndpoint` is `extra="forbid"`, so a key invented here aborts *that*
repository with a `ConnectorError` naming the file. `test_manifest_surface.py`'s module docstring
already records one such key being considered and rejected on those grounds — and until this commit
nothing on this side would have noticed somebody adding it anyway. This repository owns the
manifests, so the refusal belongs in this suite: the same argument
`D-2026-09-14-the-gate-that-catches-a-change-is-the-gate-of-the-tree-it-is-made-in` makes for a
change, applied to a declaration.

## What was kept, and one thing that was deliberately not moved

- `_REQUIRED` still exists and is now `tuple(DatasetManifest.model_fields)`. It is parametrized over
  by the enforcement test, whose own docstring records that the literal it replaced was short by
  one — so deriving it is what keeps "a seventh field is covered the day it is added" true.
- A bare `tools:` key is **coerced** to `[]` rather than refused. `tools:` with nothing under it
  means an empty list to whoever wrote it, and `assert_manifest_matches` has a far better sentence
  for "declares [] while the server serves one" than a type error does. The workaround did not
  disappear; it moved from a call site to the model, which is where a reader looks for the shape.
- **The classification rule stays in `assert_manifest_matches`.** Chemclaw3 enforces
  exactly-once classification inside its model; here it stays an assertion, because the assertions
  name the manifest path and the offending tools and a `ValidationError` would name a field. The
  model took the shape, not the policy.
- `BearerAuth` has **no `none` mode**, which is stricter than Chemclaw3's model and matches
  `CLAUDE.md`'s rule that every manifest declares bearer *including on the loopback dev URL*. That
  was a review convention and is now unrepresentable.

## What keeps it true

- `packages/mcp_server_kit/tests/test_datasets.py::test_a_misspelled_key_is_named_beside_the_field_it_makes_look_absent`
  — the defect the model exists for, asserted in both halves of the message.
- `packages/mcp_server_kit/tests/test_datasets.py::test_a_key_nothing_reads_is_refused_rather_than_ignored`
  and `test_no_shipped_manifest_carries_a_key_the_loader_does_not_read` — the refusal, and the fleet
  it was applied to.
- `packages/mcp_server_kit/tests/test_datasets.py::test_every_provenance_field_is_required` — still
  parametrized over `_REQUIRED`, which is now derived from the model.
- `packages/mcp_server_kit/tests/test_manifest_model.py::test_a_key_this_fleet_invents_is_refused_here_rather_than_at_chemclaw3_s_startup`
  — the `extra="forbid"` half, in the repository that owns the file.
- `packages/mcp_server_kit/tests/test_manifest_model.py::test_a_bare_tools_key_is_an_empty_list_rather_than_a_type_error`
  — the workaround, still true, now stated once.
- `packages/mcp_server_kit/tests/test_manifest_model.py::test_auth_cannot_be_omitted_or_declared_none`
  — the rule that was a review convention.
- `packages/mcp_server_kit/tests/test_manifest_model.py::test_every_shipped_manifest_in_this_repository_validates`
  — both manifest directories, including the `mount: backend` key nobody over there may accept.
- `packages/mcp_server_kit/tests/test_datasets.py::test_a_manifest_with_a_null_tools_key_is_named`
  — the message `assert_manifest_matches` gives for an empty declaration is unchanged.
