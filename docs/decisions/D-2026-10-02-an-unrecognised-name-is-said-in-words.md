# D-2026-10-02-an-unrecognised-name-is-said-in-words — An unrecognised name is said in words

**Status:** accepted · **Date:** 2026-10-02 · **Scope:** `servers/chem`, `resolve_compound`.

## What was found

- **A miss reached the agent as an empty string.** In the live re-verification of 2026-10-02,
  "aniline", "4-bromoanisole" and "phenylboronic acid" each came back with the audit line
  `tool resolve_compound ok` and `result_inline=""` (the SHA-256 of the empty string). The tool
  returned `None`, and FastMCP (`mcp` 1.29, `func_metadata._convert_to_content`) writes `None` as
  **zero content blocks**. `structuredContent` was `{"result": null}`, but the agent reads the text
  blocks, and `langchain_mcp_adapters` joins none into `""`. The docstring called `None` "a real
  answer", but on the wire it was silence, and silence looks the same as a broken tool. The agent
  recovered each time by sending a SMILES.
- **The three names are out of the table's scope, not gaps in it.** `bench-reagents` is a verbatim
  port of Chemclaw3's `chemclaw.core.reagents`: 61 solvents, bases, catalysts, ligands, coupling
  agents and oxidants. Substrates and building blocks are an open-ended set that no committed table
  covers. No server here may call out to a name service, so the useful answer is to say that and
  ask for a SMILES.

## What was decided

- **A miss is a worded result: `recognised: false`, the corpus searched, the reason, what the tool
  accepts, and near-miss `suggestions`.** The reason says outright that this server has no
  name-to-structure service. Suggestions come from `difflib` at a 0.8 ratio over the table's own
  spellings, so "dipaa" is offered DIPEA and "aniline" gets nothing. They are offered and never
  substituted, following "refuse rather than approximate".
- **A miss stays a result, not an error.** That keeps the earlier reasoning in `test_server.py`.
  A refusal (`isError`) is for an input that cannot be served, such as the `CO`/methanol collision.
  An unknown name is a normal answer, and an error result would be audited as a failure.
- **The declared output schema is unchanged.** The tool is annotated
  `Annotated[CallToolResult, ResolvedCompound | None]` and builds its own result. The text block
  carries the miss as JSON, and `structuredContent` stays `{"result": null}`, which validates
  against the schema Chemclaw3 already holds. Hits are byte-identical to before.
- **The table is not extended.** Adding aniline and similar substrates would make a scoped
  reagent table into an incomplete compound dictionary, and it would also break the verbatim port
  of Chemclaw3's table.

## Alternatives weighed

- **Widen the output to `ResolvedCompound | UnrecognisedCompound`.** This is cleaner for a
  structured reader. It was declined because it changes the contract on the other side of the seam,
  and no consumer reads this tool's `structuredContent`.
- **Raise `ValueError` for a miss.** This puts the words on the wire for free. It was declined
  because a miss would then be audited and counted as a tool failure.

## What keeps it true

- `servers/chem/tests/test_server.py::test_an_unknown_name_is_said_in_words_on_the_wire`
- `servers/chem/tests/test_server.py::test_the_declared_output_schema_is_unchanged`
- `servers/chem/tests/test_server.py::test_an_unknown_name_comes_back_as_a_result_not_an_error`
- `servers/chem/tests/test_tools.py::TestResolveCompound::test_a_miss_says_so_and_says_what_would_resolve`
- `servers/chem/tests/test_tools.py::TestResolveCompound::test_a_near_miss_is_offered_and_not_substituted`
