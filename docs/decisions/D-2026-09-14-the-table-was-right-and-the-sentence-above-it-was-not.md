# D-2026-09-14-the-table-was-right-and-the-sentence-above-it-was-not — The table was right and the sentence above it was not

**Status:** accepted · **Date:** 2026-09-14 · **Commit:** fix pass over the adversarial review of
`811d3de`. Corrects one sentence in `D-2026-09-14-a-row-that-cannot-occur-proves-the-other-branch`,
which is merged and therefore not edited. That record's **decision stands in full**.

## Context

`D-2026-09-14-a-row-that-cannot-occur-proves-the-other-branch` found that
`test_an_inert_consumer_run_is_not_agreement` did not hold the widening it was written for: five of
its six rows carried no `passed` count, so gutting `_INERT_OUTCOMES` back to `("skipped",)` left the
file green. That finding is correct, the fix is correct, and the record's own ASCII table
(`:30-37`) states the mechanism correctly.

The sentence two lines above that table does not:

> Five of its six rows carried **no `passed` count**, so each was decided by
> `if not counts.get("passed")` and never reached `_INERT_OUTCOMES` at all

`inert_outcome` consults `_INERT_OUTCOMES` **first**. The pass arm is the fallthrough.

### Measured

Against the shipped function at HEAD:

```
'1 xfailed in 0.31s'     -> 'reported 1 xfailed'
'1 xpassed in 0.31s'     -> 'reported 1 xpassed'
'1 skipped in 0.3s'      -> 'reported 1 skipped'
'2 deselected in 0.3s'   -> 'reported 2 deselected'
'no tests ran in 0.01s'  -> 'ran no test that passed'
''                       -> 'ran no test that passed'
```

Four of the six no-pass rows are answered **by** `_INERT_OUTCOMES`; only two reach the pass arm.
With the tuple gutted to `("skipped",)`, the first three fall through and are answered
`'ran no test that passed'` instead.

So the conclusion the record drew is right and its stated reason is wrong. The reason that carries
is the weaker, sufficient one: **a no-pass row returns non-`None` whatever `_INERT_OUTCOMES`
holds** — by one arm in the shipped implementation and by the other in the gutted one — so it cannot
tell the two apart, which is the only thing a bite test is for. The record's table says exactly that,
column by column; the prose replaced it with a cleaner story that is not what runs.

This is worth a record rather than a silent docstring edit because the false version reads
*stronger*. "Never reaches the set at all" implies the two arms partition the input, which would
make the table's own `skipped` row impossible; the true version is a fallthrough, and a reader who
believes the partition will misjudge the next change to either arm.

## Decision

The same sentence was copied into
`tests/test_consumer_agreement.py::test_an_inert_consumer_run_is_not_agreement`'s docstring, and
that is what a future reader of the check actually reads. It is rewritten to state the arm order and
the measurement rather than the story, and it says out loud that it shipped wrong — the same
treatment `D-2026-09-14-a-ratchet-that-matches-a-comment-holds-nothing` gives a corrected paragraph.

Nothing about `inert_outcome` or the table of rows changes. Both are right.

## What keeps it true

- `tests/test_consumer_agreement.py::test_an_inert_consumer_run_is_not_agreement` — unchanged in
  behaviour; its docstring now quotes the two answers a lone `1 xfailed` row gets, shipped and
  gutted, which is a claim a reader can re-drive in one line.
