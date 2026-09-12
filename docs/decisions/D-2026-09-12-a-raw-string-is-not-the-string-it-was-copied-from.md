# D-2026-09-12-a-raw-string-is-not-the-string-it-was-copied-from — A raw string is not the string it was copied from

**Status:** accepted · **Date:** 2026-09-12 · **Commit:** wave W23, on top of `91c8f6e`. No hash is
written for this pass, for the reason
`D-2026-09-12-a-bypass-that-is-not-in-the-suite-is-not-closed` §5 gives.

## Context

`servers/rxnpredict`'s Molecular Transformer tokenizer is a regular expression transcribed from
upstream MolecularTransformer, where it is written in an ordinary (non-raw) Python string. Copied
here into an `r"…"` string, one of its branches changed meaning: `\\\\` in a non-raw string is the
two characters a regex reads as *one literal backslash*; the same four characters in a raw string
are two literal backslashes, so the branch became a pattern for two consecutive backslashes, which
no SMILES contains.

W22 rewrote the function around that pattern — correctly, replacing an `assert` with a refusal,
because `python -O` deletes an `assert` and the round-trip check is the whole safety of the
tokenizer. It parametrised three gaps the pattern does not cover (`CCSeC`, `C%(123)CC`, `CCcCKC`),
all of them rare, and did not look at the branch that was already broken.

**Measured, against the shipped pattern and the corrected one:**

| SMILES | shipped | fixed |
| --- | --- | --- |
| `C/C=C/C` (trans) | round-trips | round-trips |
| `C/C=C\C` (cis) | **does not** | round-trips |
| `CC(=O)Oc1ccccc1C(=O)O` | round-trips | round-trips |
| `CCSeC`, `C%(123)CC`, `CCcCKC` | refused | refused |

The asymmetry is the shape of the defect: `/` was covered and `\` was not, and RDKit emits both. So
**half** of stereodefined alkenes reached the round-trip check as a mismatch and were refused as "a
structure this tokenizer cannot represent".

**What that looked like from outside is the part that matters.** `tools._survivors` drops a raising
predictor and refuses only when *every* one failed. A chemist asking about a cis-configured alkene
therefore got a consensus over fewer models, with no error, no warning, and `n_models_succeeded`
quietly one lower — for exactly the class of chemistry where a forward prediction's stereochemical
weakness is already the caveat the docstring leads with.

## Decision

`\\\\` becomes `\\` in `TOKEN_PATTERN`, and the reason lives beside it as a comment about the raw
string rather than as an assertion about SMILES.

`servers/rxnpredict/tests/test_tokenizer.py` gains both directions as a parametrised case, built
with `chr(92)` rather than written as an escape — **because the defect was a backslash that changed
meaning when it was transcribed between two kinds of string literal, and a test that re-transcribes
it can acquire the same bug.** Parametrised over both directions on purpose: a test using `/` alone
would have been green against the shipped pattern, and `/` is the form anybody writing an example
reaches for first.

**A second correction in the same function, which is a false reason rather than a false change.**
W22's docstring said `ValueError` was chosen because "`connector_app` passes that family through
verbatim, so the model is told this predictor cannot represent this structure". Both callers are
inside `asyncio.gather(..., return_exceptions=True)`, and `_survivors` logs the `repr` and appends
only `f"{name} ({type(result).__name__})"` to what the caller is told — deliberately, so a
predictor's own text cannot carry a checkpoint path into a context window. So the message reaches an
operator's log and never the model, and the `truncate_echo` added beside it bounds a **log line**
rather than the context window. Both are still worth having; the `-O` argument for replacing the
`assert` is untouched and still correct.

## Consequences

- **A test that must now *pass* sits beside three that must still be refused.** The refusal arms are
  what keep the fix from being read as "loosen the pattern until things stop raising".
- **The stale reason is corrected in two places**, the tokenizer's docstring and
  `test_the_refusal_does_not_echo_a_megastring_whole`'s, because both carried it.
- **Three smaller findings from the same post-merge review land here rather than in records of their
  own**, each a one-line correction with no decision in it: `tests/test_fleet.py` cited
  `no_egress.assert_no_egress` for a function named `assert_no_egress_sources`;
  `test_every_server_proves_its_bearer_check_against_a_running_server` walked every `ast.Call` in a
  file, so the call satisfying it could sit in an uncollected helper, an unconditionally skipped
  test, or dead code — it now walks only the bodies of `test_*` functions, which is the set pytest
  collects.

## What keeps it true

- `servers/rxnpredict/tests/test_tokenizer.py::test_both_halves_of_a_stereodefined_double_bond_are_covered`
  — parametrised over `C/C=C/C` and `C/C=C\C`. Reverting the pattern fails the second arm only,
  which is the asymmetry itself as a test result.
- `servers/rxnpredict/tests/test_tokenizer.py::test_a_structure_the_pattern_does_not_cover_is_refused_not_silently_shortened`
  — the three gaps that must still be refused, each asserting what would have been sent instead.
- `servers/rxnpredict/tests/test_tokenizer.py::test_the_refusal_does_not_echo_a_megastring_whole` —
  the bound, now with the right reason on it.
- `tests/test_fleet.py::test_every_server_proves_its_bearer_check_against_a_running_server` — the
  narrowed search.
