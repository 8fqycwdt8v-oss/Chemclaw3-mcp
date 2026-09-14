# D-2026-09-14-a-total-beside-a-mutation-needs-the-invocation-that-produced-it — A total beside a mutation needs the invocation that produced it

**Status:** accepted · **Date:** 2026-09-14 · **Commit:** fix pass over the adversarial review of
`811d3de`. Corrects the two figures in
`D-2026-09-14-eight-assertions-of-one-clause-is-a-choice-not-an-accident`, which is merged and
therefore not edited. Both records' decisions stand in full.

## Context

`D-2026-09-14-how-many-stayed-green-is-a-claim-about-a-commit` states the rule:

> **A driven mutation is recorded by what it reds, not by how many stayed green.** "Reds
> `test_a_suppressed_test_is_not_a_proof` and no other test in the file" is the claim; a total is a
> number no test holds and that the next commit to the file falsifies. Where a drive genuinely turns
> on a count — a parametrised id, a number of files a reader returns — the record says which
> *measurement* produces it, so a reader can re-run it.

`D-2026-09-14-eight-assertions-of-one-clause-is-a-choice-not-an-accident` was written the same day
and its `## What keeps it true` carries two totals with no invocation beside either:

> Driven: the `calc` selector drift reds it (`1 failed, 62 passed`) …
> the per-server half, driven by the same mutation on the same commit: `2 failed, 5 passed`.

That is the carve-out's *other* branch used as if it were the first: a total, not a parametrised id,
and no measurement named. `62` is additionally the figure the superseded record published, so the
number a reader recognises is the one that had already been retired once.

### Measured

Re-driven at HEAD, which is what the rule asks for and what neither record made possible:

```
$ uv run pytest -q tests/test_deploy_shape.py
63 passed in 0.65s
$ uv run pytest -q servers/calc/tests/test_deploy.py
7 passed in 0.06s
```

So `1 + 62` and `2 + 5` are both still exact, and the invocations are now on the record. They were
never wrong; they were unre-runnable, and they falsify on the next test added to either file — which
is precisely the property the sibling record objects to, arriving in its sibling.

## Decision

The claim those two drives support, restated so it survives a merge:

- **`tests/test_deploy_shape.py::test_the_egress_policy_denies_and_selects_the_workload` reds on the
  `calc` selector drift, at the `calc` parametrised id, and at no other id and no other test in the
  file.** That is a name, and a reader can check it.
- **`servers/calc/tests/test_deploy.py::test_the_pod_label_matches_the_networkpolicy_selector` reds
  on the same mutation**, beside that file's own `test_egress_is_denied`, which is why its drive
  reported two failures rather than one.

Where a total is worth writing at all, the record writes the invocation beside it, as the block
above does. A figure with no command is a claim about its author's terminal — the argument
`CLAUDE.md` makes three times about counting things, applied to a pytest summary line.

Nothing in either file changes. Neither record's conclusion is affected: the mutation does red the
tests named and nothing else.

## What keeps it true

- `tests/test_deploy_shape.py::test_the_egress_policy_denies_and_selects_the_workload` — the
  fleet-wide half, unchanged; the invocation that produces its total is now recorded above.
- `servers/calc/tests/test_deploy.py::test_the_pod_label_matches_the_networkpolicy_selector` — the
  per-server half, unchanged, likewise.
