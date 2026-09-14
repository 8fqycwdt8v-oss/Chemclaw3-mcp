# D-2026-09-14-a-degraded-answer-is-counted-once — A degraded answer is counted once

**Status:** accepted · **Date:** 2026-09-14 · **Commit:** fix pass over W26/W27/W29, on top of
`f3f3c9c`. Revisits `D-2026-09-12-a-degradation-that-is-not-counted-is-a-degradation-nobody-sees`
and `D-2026-09-13-a-cache-key-derived-from-text-nobody-validated-is-not-a-key`, both of which stand.

## Context

`chemclaw_mcp_degraded_total` is documented as counting **answers returned with a component's
contribution missing**. `rxnpredict`'s prediction cache classifies a canonicaliser that raises
anything but `ValueError` — RDKit absent, the egress guard refusing a library's lookup, an
allocation failing — and records one increment per classification.

It recorded per *key derivation*, and a prediction derives its key twice:
`BaseForwardPredictor.predict` called `cache.get_forward(...)` on the way in and
`cache.set_forward(...)` on the way out, and each of those derived the key from scratch.
`_key_conditions` compounded it by canonicalising both the reactant side and the product side
before testing either.

### Measured, on the shipped `predict()`

A stub predictor that always answers, with the canonicaliser raising `EgressForbidden`:

```
forward:    result_len=1 entries=0 delta=2.0
conditions: result_len=1 entries=0 delta=4.0
```

One prediction, one degraded answer, two (or four) increments — and a WARNING line each. The
consensus tools fan out over every enabled predictor, measured at six at once in
`D-2026-09-12-one-tool-call-is-not-one-thread`, so one tool call on an image without RDKit published
**12 to 24**. A rate over that series is then a rate of key derivations, which is a number nobody
asked for and which changes if the cache is refactored.

### Why the test did not see it

`test_a_broken_canonicaliser_is_classified_and_counted` asserted `+1.0` after calling `set_forward`
**alone**, while its own docstring said it was "driven through the shipped `set_forward` … so the
assertion covers the wiring as well as the branch". The wiring makes two calls. Asserting `+1.0` on
one of them is a test that is right about a half it chose.

## Decision

**A prediction derives its key once, and the cache's surface is shaped so there is no second way.**
`PredictionCache` exposes `key_forward` / `key_conditions` and `get` / `set`; the four combined
methods are gone. `predict()` derives the key, uses it for the lookup, and passes the same key to
the store.

That is the root cause rather than the symptom: the double count was one consequence, and
canonicalising the same reaction twice on every cache miss — RDKit work on the hot path — was the
other. There is no convenience pair left for a future caller to reach for.

**`key_conditions` short-circuits on the reactants.** Two SMILES are one answer, and the second
failure teaches nobody anything the first has not already logged and classified.

The counting rule this settles on, stated so the next component can be held to it: **one answer a
server returns with something missing is one increment**, whatever number of internal operations
noticed the same breakage.

### What this deliberately does not change

The `ValueError` arm still moves no counter — a caller's typo is not this pod degrading — and the
cache still never fails a prediction. Both were re-driven and hold.

## What keeps it true

- `servers/rxnpredict/tests/test_cache.py::test_a_broken_canonicaliser_is_classified_and_counted`
  — drives `BaseForwardPredictor.predict`, not a cache method, for all three causes. Driven: making
  the store re-derive its key (the shipped form) turns all three red at 2.0. **Its name is
  unchanged on purpose**: `D-2026-09-13-a-cache-key-derived-from-text-nobody-validated-is-not-a-key`
  cites it, a merged record is never edited, and `tests/test_decision_log.py` is what caught the
  rename. The name is still true of what it asserts; what changed is what drives it.
- `servers/rxnpredict/tests/test_cache.py::test_a_conditions_prediction_counts_one_even_though_it_has_two_smiles`
  — the 4x arm, over `BaseConditionsPredictor.predict`. Driven: restoring the both-sides-then-test
  form turns both red.
- `servers/rxnpredict/tests/test_cache.py::test_a_caller_typo_moves_no_degradation_counter` — the
  arm that must stay silent, unchanged.
