# D-2026-09-13-a-hand-compiled-rule-table-is-a-table-with-a-typo-in-it — A hand-compiled rule table is a table with a typo in it

**Status:** accepted · **Date:** 2026-09-13 · **Commit:** wave W26, on top of `0fb12537`.
No hash is written for this pass, for the reason
`D-2026-09-12-a-bypass-that-is-not-in-the-suite-is-not-closed` §5 gives.

## Context

The wave row that produced this record asked for the **`rxno_id` named-reaction → ontology table**
in `rxnlabel` to be validated against itself. **That table does not exist, and never has.**
Measured: `rxno_id` appears four times in this repository — a `str | None` field on
`NamedReactionMatch`, two paragraphs saying the server will never populate it, and
`servers/rxnlabel/tests/test_tools.py:126`, `assert found.rxno_id is None, "this server never
invents an ontology id"`. `git log -S rxno_id` finds it only in the commit that added the server.
The decision the row wanted made was already made, in the opposite direction and for the row's own
reason: "a wrong `rxno_id` is worse than none".

What the row is right about is the *server* and the *failure shape*. `rxnlabel` is the one server
in this fleet with hand-compiled tables and no self-validation at all — it has no vendored corpus,
so it has no `test_dataset.py`, and its three tables live in `engine/agents.py` as code:

| Table | What it decides |
| --- | --- |
| `TRANSITION_METALS` (28 symbols) | whether a reaction is metal-catalysed, which gates every ligand rule |
| `_SOLVENT_SMILES` (40 tokens) | whether a species is a solvent |
| `_LIGAND_SMARTS` (7) + `_BASE_SMARTS` (13) | whether a species is a ligand or a base |

Both mechanisms around them fail **quietly**. `_matches_any` skips a SMARTS that will not compile;
`_canonical_or_none` drops a solvent token that will not parse. A dead rule and an absent rule are
indistinguishable from outside, because a species that matches nothing is labelled `additive` — a
label, not a blank.

That is not hypothetical here. The bicarbonate pattern shipped as `[OX2H0-][CX3](=O)[OX2H0]`,
demanding a two-connection anionic oxygen that bicarbonate does not have, and matched no `NaHCO3`
written any way — one of the commonest bases in a coupling corpus, silently absent from every
frequency table this server feeds. It was found by reading, in a later wave.

And `_matches_any`'s own docstring said the leniency was safe because "the server's own tests assert
each pattern individually". **Measured false**: `servers/rxnlabel/tests/test_roles.py` parametrises
over `species.FUNCTIONAL_GROUPS` and touches neither `_LIGAND_SMARTS` nor `_BASE_SMARTS`.

### What the audit found

Running the checks below against the tables as they stood:

| Check | Result |
| --- | --- |
| every ligand/base SMARTS compiles | 20/20 pass |
| every solvent token parses | 40/40 pass |
| **no two solvent tokens are the same molecule** | **FAIL — 40 tokens, 39 molecules** |
| every symbol in `TRANSITION_METALS` is d-block | 28/28 pass |
| every base rule matches the reagent its comment names | 20/20 pass |
| every ligand rule matches the reagent its comment names | 7/7 pass |
| the species each rule was narrowed to exclude stay excluded | 6/6 pass |

The one failure is the ester row: `CCOC(C)=O CC(=O)OC COC(C)=O` — ethyl acetate, then **methyl
acetate written twice**. A `frozenset` swallows it, so the table looked one entry longer than it
was. The cost is not the duplicate; it is whichever solvent the second slot was meant to hold, which
nothing records and this record does not guess at.

One observation recorded rather than acted on: `Cd` and `Hg` are in `TRANSITION_METALS` while `Zn`
is out, though all three are group 12. `Zn`'s exclusion is argued in the comment beside the set — a
Grignard-style organometallic partner is a reagent, not a catalyst — and `Cd`/`Hg` are not species a
process ELN contains. Changing the chemistry is outside this wave; the asymmetry is noted so the
next reader does not have to re-derive it.

## Decision

Delete the duplicate token, and hold all three tables with a test file built on
`servers/props/tests/test_dataset.py`'s pattern: **every assertion is an independently written
structure or an independently derived fact that must agree with the table, never a re-typing of the
table's own text.**

- The reagent SMILES are typed from the *names in the comments* beside each rule. `agents.py`
  carries SMARTS and this file carries SMILES, so a character copied between them would not be
  well-formed in the other.
- The element facts come from **RDKit's periodic table by atomic number** — groups 3-12 as three
  runs — not from a second copy of `TRANSITION_METALS`. That is the check that catches a
  transposition between two *real* symbols (`Rb` for `Rh`), which spell-checking a list against
  itself cannot see.
- The solvent assertions use a **different spelling** from every token in the table, so what is
  tested is canonicalisation-then-membership rather than string equality with the source.
- The negative controls are the species the comments say each rule was narrowed to exclude — HOBt,
  the triazoles, `KH2PO4`, a substrate amine. They are what stops the file passing on a rule that
  matches everything.

`_matches_any` keeps its lenient skip — failing every classification in a corpus because one pattern
has a typo is the worse failure — and its docstring now names the test that makes the leniency safe
instead of claiming a test that did not exist.

## What keeps it true

- `servers/rxnlabel/tests/test_agent_tables.py::test_every_role_smarts_compiles` — the 20 patterns
  `_matches_any` would otherwise skip in silence.
- `servers/rxnlabel/tests/test_agent_tables.py::test_every_base_rule_recognises_the_reagent_its_comment_names`
  — the bicarbonate defect generalised to every row. Restoring the historical broken pattern fails
  it.
- `servers/rxnlabel/tests/test_agent_tables.py::test_a_rule_narrowed_to_exclude_something_still_excludes_it`
  — the other direction; loosening the pyridine rule to a bare `[nX2]` fails it on HOBt.
- `servers/rxnlabel/tests/test_agent_tables.py::test_every_ligand_rule_recognises_the_reagent_its_comment_names`
  — the same audit for `_LIGAND_SMARTS`, with the metal those rules require present.
- `servers/rxnlabel/tests/test_agent_tables.py::test_no_two_solvent_tokens_are_the_same_molecule`
  — the measured defect, written as token-count against set size so it stays true as the list grows.
- `servers/rxnlabel/tests/test_agent_tables.py::test_every_solvent_token_parses` — the neighbouring
  silent drop.
- `servers/rxnlabel/tests/test_agent_tables.py::test_a_solvent_is_recognised_however_it_is_spelled`
  — that the table is consulted canonically and not as text.
- `servers/rxnlabel/tests/test_agent_tables.py::test_every_transition_metal_symbol_is_a_d_block_element`
  and `test_the_main_group_metals_the_comment_excludes_are_still_excluded` — the set, in both
  directions, against a basis that is not a copy of it.
