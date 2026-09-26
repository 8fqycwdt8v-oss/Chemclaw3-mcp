# D-2026-09-26-one-echo-bound-and-the-refusals-that-bypassed-it — One echo bound, and the refusals that bypassed the four copies of it

**Status:** accepted · **Date:** 2026-09-26 · **Builds on:**
`D-2026-09-16-a-bound-with-no-off-refuses-at-import-in-one-place` (one config-driven bound, read
through `env_bound`), and `D-2026-09-15-five-copies-varied-the-message-not-the-mechanism` (a
mechanism copied per server is extracted once into `mcp_server_kit.limits`).

## What was found

`chem`, `calc`, `safety` and `rxnpredict` each declared a 120-character `_MAX_ECHO_CHARS` and an
`_echo`/`truncate_echo` beside it, because `connector_app` passes a `ValueError` to the model
verbatim: an unbounded echo is unbounded caller-influenced text in the context window of the turn
that asked. The copies bounded the parse-failure sites written beside them and nothing else. The
backlog row's grep, `\{[a-z_]*\.?smiles[^}]*!r\}` over the serving trees, found fifteen raises
interpolating a structure raw, in `calc` (`pka`, `logd`, `descriptors`, `structure`, `xtb_engine`)
and `chem` (`species`, `cleavage`, `depiction`). Measured 2026-09-12: `predict_pka` on `"C" * 1500`,
inside both structural bounds and so an ordinary accepted call, raised a 1,587-character refusal.
The ceiling over those sites was `MAX_SMILES_CHARS`, 4,000 characters, which exists to stop a parse
and not to shape a message.

Reading the tree rather than grepping it found the same shape outside the SMILES vocabulary: a
caller's solvent name in `props`' `records.require`, `calc`'s `solvents` and `xtb_engine` and
`chem`'s `stoichiometry`, and a caller's formula in six `thermalsafety` `oxygen_balance` refusals.

## What was decided

- **`mcp_server_kit.limits.echo` and `MAX_ECHO_CHARS` (`MCP_MAX_ECHO_CHARS`, default 120, floor 1)
  replace the four copies.** Read through `env_bound` like every other bound in that module, so it
  refuses at import rather than silently turning a refusal into an ellipsis and a count, and the
  fleet's derived bound inventory picks it up with nothing registered by hand.
- **Every caller-derived interpolation in a raise goes through it**: the fifteen structure sites,
  the solvent and name sites, and the formula sites.
- **A static scan holds the rule, and its reach is a vocabulary, stated as one.** An f-string inside
  a `raise` that interpolates a bare name containing `smiles`, an attribute containing `smiles`, or a
  bare `formula`, `solvent` or `name` must go through `echo`, or be argued in
  `ECHO_IS_NOT_CALLER_DERIVED` beside the test. Seven are argued: `connector_app`'s own server name,
  the two reagent tables' own SMILES in a corpus-indexing error, `env_bound`'s variable name, a
  `thermalsafety` parameter name spelled by code, and two self-tests over published fixtures. What
  the scan cannot see is a value spelled outside that vocabulary and a message built into a variable
  before the `raise`; both are written into the test's comment rather than implied.

## What keeps it true

- `tests/test_fleet.py::test_no_refusal_interpolates_caller_text_past_the_echo_bound`
- `tests/test_fleet.py::test_the_argued_echoes_are_still_there`
- `tests/test_fleet.py::test_the_echo_scan_reads_a_tree_and_not_the_text`
- `packages/mcp_server_kit/tests/test_limits.py::test_an_echo_past_the_bound_keeps_the_head_and_names_the_length`
- `packages/mcp_server_kit/tests/test_limits.py::test_the_echo_bound_is_the_environment_s`
- `servers/calc/tests/test_smiles_bounds.py::test_a_refusal_of_an_accepted_structure_is_echoed_bounded_too`
