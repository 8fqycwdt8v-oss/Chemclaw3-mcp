# D-2026-09-13-a-cache-key-derived-from-text-nobody-validated-is-not-a-key — A cache key derived from text nobody validated is not a key

**Status:** accepted · **Date:** 2026-09-13 · **Commit:** wave W26, on top of `0fb12537`.
No hash is written for this pass, for the reason
`D-2026-09-12-a-bypass-that-is-not-in-the-suite-is-not-closed` §5 gives: the work merges by squash,
so any hash this session could name is a branch commit `main` will not contain.

## Context

`servers/rxnpredict/engine/cache.py` keyed a prediction on the *canonical* reactants (and product),
so two spellings of one reaction share a slot. Canonicalisation was wrapped:

```python
def _safe_canon_reactants(smiles: str) -> str:
    try:
        return canonical_multi_smiles(smiles)
    except Exception:
        return smiles
```

The module docstring called that "best-effort … because a cache must never be the thing that fails
a prediction", which is a true sentence about the wrong mechanism. Nothing validates `reactants`
before it reaches here — `tools.py::predict_products` passes the caller's string straight to
`p.predict(reactants, top_k)` — so the `except` arm is reachable from an ordinary tool call.

Driven against the shipped code in this tree:

| # | Driven | Result |
| --- | --- | --- |
| 1 | `set_forward("m", "CCO.Xx9nope", …)` then `get_forward("m", "Xx9nope.CCO", …)` | **MISS** — one set of molecules, two entries |
| 2 | `_safe_canon_reactants("C" * 5000)` | returns the 5,000-character string unchanged |
| 3 | `_safe_canon_reactants("C" * 3000)` | returns it unchanged |
| 4 | canonicaliser raising `ImportError` / `EgressForbidden` / `MemoryError` | all three return raw text |
| 5 | `chemclaw_mcp_degraded_total` after 1-4 | **no series at all** |

Three separate defects.

**1 is the key not being a key.** `canonical_multi_smiles` sorts its components and raw text does
not, so a partially-invalid input mints one entry per spelling. Nothing is *corrupted* — the key is
a SHA-256 over the parts, so two molecules never collide — but an identity derived from text
nothing checked is a claim the cache is not entitled to make.

**2 and 3 are two deliberate refusals swallowed.** `mcp_server_kit.limits` exists because
`MolToSmiles` on a large enough molecule overflows the C stack and kills the pod with an uncatchable
SIGSEGV; `canonical_smiles` raises *before* parsing for that reason. Both bounds arrive as a
`ValueError` and became a cache row keyed by the very string the bound rejects.

**4 and 5 are the fleet-wide claim being false here.** `CLAUDE.md` says "every path in this fleet
that catches an exception and answers anyway now classifies it through
`mcp_server_kit/degradation.py`". This path did not, and `EgressForbidden` is the case that module's
own docstring says it exists for: it subclasses `OSError`, so a coarse handler buries it, and this
was the coarsest handler there is.

## Decision

**An input this server will not canonicalise is not cached**, and the two arms of "will not" are
answered differently.

`_canonical_or_none(canonicalise, smiles)` returns the canonical form or `None`; `_key_forward` and
`_key_conditions` return `str | None`; `_get` misses on `None` and `_set` is a no-op on it. The
prediction still runs. A cache never fails a prediction — it declines to claim it recognised one,
which is `CLAUDE.md`'s "refuse rather than approximate" applied to an identity rather than to an
answer.

- **`ValueError`** — an unparseable SMILES, or one over `limits`' character or atom bound — is a
  fact about the caller's argument, not about this pod. Logged at DEBUG with the string truncated
  (the over-length case is exactly what reaches here) and **no metric**. Counting it would make
  `chemclaw_mcp_degraded_total`, the series that means a component of this server has gone missing,
  fire on every typo, and a signal that fires on typos is not a signal.
- **Anything else** is this pod: RDKit absent from the image, the egress guard refusing a library's
  lookup, an allocation failing. Classified through `degradation.classify` and counted under a new
  registered component, `prediction_cache`, so it is visible from a scrape.

The component name is registered through `degradation.register_components` at import, because
`record` clamps an unregistered one onto `<unknown>` rather than minting a series for it.

### What this costs

Caching is lost for any input RDKit refuses and a predictor accepts — the sequence models will
happily tokenise garbage. That is the intended trade: the entry it bought was keyed by a string
nobody had checked, and the win the cache exists for is a repeated call inside one conversation
about a real reaction.

### What was rejected

**Keying the fallback under a `"raw"` namespace tag.** It removes any cross-namespace collision and
keeps the entry, but it does not fix defect 1 (unfixable when there is no canonical form to sort),
it keeps an unvalidated key, and it adds a code path where this decision removes one.

## What keeps it true

- `servers/rxnpredict/tests/test_cache.py::test_an_unparseable_input_is_declined_rather_than_keyed_by_its_raw_text`
  — both halves together: the call does not raise *and* nothing is stored. The test it replaces
  asserted only the first, which is how the raw-text fallback shipped.
- `servers/rxnpredict/tests/test_cache.py::test_one_molecule_set_spelled_two_ways_never_mints_two_entries`
  — defect 1 in the case that produced it.
- `servers/rxnpredict/tests/test_cache.py::test_a_smiles_over_the_structural_limit_is_not_keyed_by_the_string_the_limit_rejects`
  — defects 2 and 3.
- `servers/rxnpredict/tests/test_cache.py::test_a_broken_canonicaliser_is_classified_and_counted`
  — defects 4 and 5, parametrised over the three exceptions measured above and driven through the
  shipped `set_forward` rather than through the helper, so it covers the wiring as well as the
  branch.
- `servers/rxnpredict/tests/test_cache.py::test_a_caller_typo_moves_no_degradation_counter`
  — the asymmetry, summed over every cause so it cannot pass by the counter moving on one this test
  did not name.
- `servers/rxnpredict/tests/test_cache.py::test_two_spellings_of_one_reaction_share_a_slot`
  — unchanged, and what stops the fix being "never cache anything".
