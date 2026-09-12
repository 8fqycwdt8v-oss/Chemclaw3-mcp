# D-2026-09-12-an-assert-is-a-control-with-an-off-switch — An `assert` is a control with an off switch

**Status:** accepted · **Date:** 2026-09-12 · **Commit:** wave W22, on top of
`3ca770a`. **No hash is written for this pass**, for the reason
`D-2026-09-12-a-bypass-that-is-not-in-the-suite-is-not-closed` §5 gives.

## Context

`servers/rxnpredict`'s Molecular Transformer tokenizer enforced its one safety property with an
`assert` over caller-derived data, and interpolated the caller's raw SMILES into the message:

```python
tokens = re.findall(pattern, smiles)
assert "".join(tokens) == smiles.replace(" ", ""), f"tokenizer drift: {smiles!r}"
```

The property is not decorative. The tokenizer is a regular expression and a character it does not
cover is not reported — `re.findall` simply returns fewer tokens. Measured against that pattern on
this pass:

```
CCSeC      → CCSC        a selenoether reaches the model as a thioether
C%(123)CC  → C(123)CC    the three-digit ring-closure form loses its %
CCcCKC     → CCcCC       any character with no branch is dropped
```

So the failure the round trip catches is the worst shape a chemistry tool has: a confident answer
about a *different molecule*. RDKit canonicalisation upstream removes the realistic instances — it
writes `CC[Se]C` — which is why this has never been observed, and is not a reason the check may
evaporate.

Three things are wrong with enforcing it by `assert`, and they are independent:

1. **`python -O` deletes it.** No file in this repository sets `PYTHONOPTIMIZE` and no
   `Containerfile` passes `-O`; the flag belongs to whoever starts the process, so the control's
   existence is a property of the platform rather than of the code. A control an operator cannot be
   told exists is not one.
2. **`AssertionError` is not `ValueError`.** `connector_app` replaces every non-`ValueError` with an
   `error_id` before the model sees it, so the agent is told nothing it can act on about an input it
   could have fixed.
3. **The message is written as a debugging aid.** It echoed the caller's SMILES whole, past the
   120-character truncation four engines in this fleet declare for precisely this reason
   (`connector_app` passes a `ValueError` to the model verbatim, so an unbounded echo is unbounded
   caller-influenced text in the turn's context window). `AssertionError` happened to keep it away
   from the model — and never away from the log.

## Decision

**In serving code an invariant is an `if` and a `raise`.** The tokenizer refuses with a `ValueError`
naming what it cannot represent, truncated through this server's own `truncate_echo`; the refusal is
caller-actionable, so `ValueError` is right rather than merely convenient.

The inventory was re-derived rather than taken on trust, and it differed from the brief that
prompted this in one place: `mcp_server_kit/no_egress.py` carries an `assert` too, and it belongs in
the same exemption as `testing.py` for the same reason. Both are imported by tests and by nothing
else — `assert_manifest_matches`, `assert_bearer_is_enforced` and `assert_no_egress` exist to *fail
a test* — so an `assert` there is the verdict, not a control.

One case beyond the tokenizer was left to decide and is converted rather than exempted:
`servers/calc/.../engine/logd.py` asserted `mol is not None` after reparsing a string `predict_pka`
had already canonicalised. The argument for keeping it was that the reparse cannot fail; the
argument against is that `servers/calc/.../engine/descriptors.py` does the identical
`MolFromSmiles`-cannot-fail check with `if mol is None: raise`, in the same server, three modules
over. Two spellings of one idiom is the thing worth removing, and the one that survives should be
the one that survives `-O`. Nothing in the fleet now asserts outside the two helper modules, which
makes the rule a rule rather than a policy with a growing list of exceptions.

**The rule is a test, not a sentence**, because a sentence is what this repository keeps finding on
the wrong side of its own code. `test_no_serving_module_enforces_an_invariant_with_assert` walks
every `src/` tree — globbed, so a new package or server is scanned the day it appears — and reads an
AST rather than text, for `no_egress.py`'s reason one layer over: an `assert` in a docstring, a
comment or a string literal reads identically as text and not at all as a tree. That distinction is
itself driven, by a bite test over a synthetic tree, so the scan cannot quietly become a `grep`.

## Consequences

- **A `ValueError` is now reachable from `_tokenize_smiles` where an `AssertionError` was.** That is
  a deliberate widening of what the model sees: the point is that it is told this predictor cannot
  represent this structure. `connector_app`'s sanitiser still covers everything else.
- **The tokenizer's pattern is a module constant.** It was rebuilt inside the function, and the test
  that drives the drift cases needed it; a second transcription would have been a second claim about
  what this model accepts.
- **The rule does not reach the wider echo problem, and that stays open.** Fourteen refusal sites in
  `chem` and `calc` interpolate a caller-derived structure with no truncation at all, bounded only
  by `mcp_server_kit.limits.MAX_SMILES_CHARS` — measured 2026-09-12, `predict_pka` on `"C" * 1500`
  (an ordinary accepted call, inside both structural bounds) raises a 1,587-character refusal where
  `_echo` would have produced about two hundred. That is a `docs/BACKLOG.md` row rather than this
  decision, because it turns on a question this one does not answer: whether the truncation belongs
  in `mcp_server_kit.limits` beside the bounds it pairs with, and whether anything can tell a
  caller-derived echo from a corpus-derived one.

## What keeps it true

- `tests/test_fleet.py::test_no_serving_module_enforces_an_invariant_with_assert` — the rule, over
  every `src/` tree, with the two helper modules exempt by name and by argument.
- `tests/test_fleet.py::test_the_assert_scan_reads_a_tree_and_not_the_text` — the bite test, so the
  scan above cannot decay into a `grep` or pass over a tree with no Python in it.
- `servers/rxnpredict/tests/test_tokenizer.py::test_a_structure_the_pattern_does_not_cover_is_refused_not_silently_shortened`
  — the refusal, with the silently-shortened string it replaced asserted beside it.
- `servers/rxnpredict/tests/test_tokenizer.py::test_the_refusal_does_not_echo_a_megastring_whole`
  and `test_a_covered_structure_round_trips_to_spaced_tokens` — the bounded message, and the success
  path without which a function that always raised would pass.
