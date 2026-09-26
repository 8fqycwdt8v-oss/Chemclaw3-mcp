# D-2026-09-26-a-class-prior-adjusts-the-corpus-it-does-not-replace-it — A per-class trust prior adjusts the corpus; it does not replace it

**Status:** accepted · **Date:** 2026-09-26

## What was found

`D-2026-09-26-an-environment-prior-adjusts-the-table-it-does-not-replace-it` made
`CHEMCLAW_RXNPREDICT_MODEL_TRUST_PRIORS` an adjustment and left
`CHEMCLAW_RXNPREDICT_MODEL_TRUST_PRIORS_BY_CLASS` as a whole-table override of the vendored
`data/trust_priors.json`: `Settings.class_priors()` returned the override if it was non-empty and
the corpus otherwise. So one class named in the variable dropped every other class's calibrated
weights, and its parser was `json.loads` and nothing else — a misspelt class label or predictor id
set nothing and read as done, `"other"` (a class `effective_prior` never selects a per-class
weight for) was accepted, and a zero or negative weight silenced or inverted a vote. The corpus
ships `{}` today, so the drop is latent; it becomes real on the first calibration a person merges.

## What was decided

The row offered two answers: merge like the global table, or stay a whole replacement that at least
validates. **Merge**, for the reason the global decision gave — an operator adjusting one weight
should change one weight — and because a replacement is still expressible, class by class, by
naming every predictor.

- **The override is laid over the corpus at `(class, predictor)` granularity** in
  `class_priors()`: each pair it names replaces that calibrated weight, and every pair and every
  class it does not name stands. It builds a new dict each call, so the `lru_cache`d corpus is never
  edited in place. The corpus is still read there and not at settings load, so a checksum failure
  stays a 503 on the probe
  (`D-2026-09-18-a-corpus-that-cannot-be-read-is-a-probe-s-answer-not-an-import-error`).
- **What the aggregator cannot mean is refused at settings load, by name**: a class label outside
  `classifier.ALL_CLASSES`, and `CLASS_OTHER` inside it; and, per class, exactly what the global
  table refuses — an unknown predictor id, a non-number, a weight not finite and above zero. The two
  tables now share one validator (`_predictor_weights`), so they cannot come to disagree about what
  a weight is.

No setting was renamed or removed. A deployment that set
`CHEMCLAW_RXNPREDICT_MODEL_TRUST_PRIORS_BY_CLASS` to a table naming classes the corpus also
calibrates now keeps the corpus's weights for the pairs it did not name, and one whose value names
`other`, an unknown class or an unknown predictor now fails to start with the reason.

## What keeps it true

- `servers/rxnpredict/tests/test_trust_priors.py::test_a_class_prior_adjusts_the_corpus_rather_than_replacing_it`
- `servers/rxnpredict/tests/test_trust_priors.py::test_no_class_override_is_the_corpus_itself`
- `servers/rxnpredict/tests/test_trust_priors.py::test_a_class_prior_the_aggregator_cannot_mean_is_refused`
- `servers/rxnpredict/tests/test_trust_priors.py::test_an_env_prior_the_aggregator_cannot_mean_is_refused`
