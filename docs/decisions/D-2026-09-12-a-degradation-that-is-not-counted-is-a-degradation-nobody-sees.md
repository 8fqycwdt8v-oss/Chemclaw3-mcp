# D-2026-09-12-a-degradation-that-is-not-counted-is-a-degradation-nobody-sees — A degradation that is not counted is a degradation nobody sees

**Status:** accepted · **Date:** 2026-09-12 · **Commit:** wave W25, on top of `285c96d`. **No hash
is written for this pass**, for the reason `D-2026-09-12-a-bypass-that-is-not-in-the-suite-is-not-closed`
§5 gives: the work merges by squash, so any hash this session could name is a branch commit `main`
will not contain, and `test_every_commit_the_registers_cite_is_reachable_from_head` refuses one.

## Context

Four `except Exception` blocks in this fleet turned a broken component into a plausible answer. The
anchors, verified rather than trusted — the brief's fourth had drifted from `:134` to `:158`:

| Site | What it swallowed into | Signal before |
| --- | --- | --- |
| `rxnlabel/engine/mapping.py:52` (`map_reaction`) | `None` — the same value "no mapper installed" gives | one WARNING naming neither reaction nor fault |
| `rxnlabel/engine/mapping.py:158` (`_mapper`) | `None`, so `labeller_version` says `mapper@absent` | `logger.exception` |
| `rxnlabel/engine/naming.py:73` (`name`) | `Naming()` — **byte-identical to the commonest correct answer this server gives** | one WARNING |
| `rxnpredict/engine/predictors/__init__.py:92` (`discover_predictors`) | a quieter ensemble | one WARNING per predictor |

No counter on any of them. Measured against a namer that imported cleanly and raised on every
reaction — the shape of a corrupt rule table, and of a loader the egress guard refuses:

```
healthz:       200 {"status":"ok","server":"rxnlabel",...}
name_reaction: {"named_reaction":null, ..., "version":"rxnlabel@2:rdkit@2026.3.5:mapper@absent:namer@0.1.3"}
/metrics:      no chemclaw_mcp_degraded_* series at all
```

**The stamp is the part that does not heal.** `engine/version.py` opens by stating its own rule —
the string names "every component whose output survives into a label, and nothing that does not" —
and its two `absent` cases exist precisely so "the corpus repairs itself the moment they arrive". A
namer that ran and raised contributed nothing and was stamped with its version anyway, so the row
equalled a *healthy* pod's stamp: no drain would ever re-derive it. A pod broken for a week writes a
week of rows that are permanently indistinguishable from good ones.

**`EgressForbidden` is why this needed a vocabulary and not a boolean.** `CLAUDE.md` already argues
it: the class subclasses `OSError` so it surfaces where a connection error would, and "any library's
own `except OSError: retry` swallows it whole". Every one of the four sites above is such a swallow.
A refusal raised inside a model loader is therefore the most likely way this fleet's whole
no-egress posture becomes invisible — `chemclaw_mcp_egress_refused_total` fires, the call returns
something, and nothing downstream says the answer is short of a component.

## Decision

**One vocabulary in `mcp_server_kit/degradation.py`: a clamped cause, a counter, and a classifier
that sorts the refusal first.**

- **`chemclaw_mcp_degraded_total{server, component, cause}`.** No actor, no session, no correlation
  id, no tool argument — the rule `metrics.py` states. `cause` is one of four constants in that
  file. `component` is a module-level constant at every call site (`mapping.COMPONENT`, a predictor's
  registry name), never a value a request can influence; `record` refuses an unclamped `cause`
  rather than minting a series for it, because `/metrics` is unauthenticated and an unbounded label
  is a series per string whoever reaches the pod invents.
- **`classify` tests `EgressForbidden` before anything else.** Written in the obvious order it would
  sort as `failed` beside a connection reset, which is the defect this record exists for.
- **The resource branch matches a *type name*, not a message.** `torch.cuda.OutOfMemoryError` is a
  `RuntimeError` subclass whose text has moved across releases, and this package must not import
  torch. So `{"MemoryError", "OutOfMemoryError"}`, and everything else torch raises is `failed` on
  purpose with the `repr` in the log. A wrong cause is worse than a coarse one, because
  `resource_exhausted` is the one cause a readiness check is forbidden to act on.
- **The answer says so too, and the stamp stops lying.** `ReactionRepresentation.degraded` and
  `ReactionNaming.degraded` name the components that ran and failed, and
  `version.labeller_version(failed=...)` writes `mapper@failed` / `namer@failed` — a **third** word,
  distinct from a version and from `absent`, so the row is stale against a healthy pod and legible to
  a chemist reading it directly. Measured after: `"...:namer@failed"` with `degraded:
  ["reaction_namer"]`, and `chemclaw_mcp_degraded_total{cause="failed",component="reaction_namer",
  server="rxnlabel"}` moving.
- **`rxnpredict` funnels through `mark_unavailable` rather than through the one anchor.** Each
  predictor module guards its own optional import and calls that function, so counting only at
  `discover_predictors`'s catch-all would have missed eleven of the twelve losses. The exception is
  passed in and classified rather than its reason text being read, because every module writes
  "missing optional deps" whatever happened — which is also what `engine/readiness.py` needs to tell
  a deployment's decision from a broken image.
- **`_survivors` counts each predictor that failed at request time.** Its refusal fires only when
  *every* predictor failed; four of five failing stayed an `outcome="ok"` tool call visible in no
  metric.

### What this costs a scrape

A normal dev pod now mints ten `not_installed` series at import. That is deliberate — an absent
extra is a real statement about what this deployment answers with — and it is one increment at
startup, so the alert an operator wants is
`rate(chemclaw_mcp_degraded_total{cause=~"egress_refused|failed"}[5m]) > 0`, not a bare rate over
the whole metric.

### What was rejected

**Raising instead of degrading.** The batch tools exist because a corpus-labelling drain sends two
hundred reactions at a time, and RXNMapper genuinely raises on inputs it cannot tokenise. Failing
the batch for one such input is the worse answer, and the original comments are right about that.
The defect was never the catching; it was that nothing downstream could tell the two apart.

**Folding `type(exc).__name__` into the label.** Bounded by whatever a dependency decides to raise,
which is not a bound anybody here controls.

## What keeps it true

- `packages/mcp_server_kit/tests/test_degradation.py::test_a_real_egress_refusal_is_not_sorted_as_a_generic_failure`
  — the ordering, driven through a **real** refusal from the armed guard rather than a constructed
  exception, with a plain `OSError` as the counterfactual.
- `packages/mcp_server_kit/tests/test_degradation.py::test_an_out_of_memory_is_separated_from_a_broken_checkpoint`
  — the split the readiness checks act on, including the torch-shaped `RuntimeError` subclass.
- `packages/mcp_server_kit/tests/test_degradation.py::test_a_missing_distribution_is_not_a_fault`
  — an absent extra is not a broken image.
- `packages/mcp_server_kit/tests/test_degradation.py::test_an_unclamped_cause_is_refused_rather_than_published`
  — the label rule, asserting also that the refused cause minted no series on the way out.
- `packages/mcp_server_kit/tests/test_degradation.py::test_recording_moves_the_series_an_operator_scrapes`
  — the counter, which is the whole point.
- `servers/rxnlabel/tests/test_degradation.py::test_a_namer_that_raises_is_not_a_reaction_that_matched_nothing`
  — the worst of the four: it asserts the nulls *are* there (the premise) and that `degraded` and
  the stamp separate them from a clean miss.
- `servers/rxnlabel/tests/test_degradation.py::test_a_mapper_that_raises_does_not_stamp_the_row_as_a_deployment_without_one`
  — including that the stamp names *which* component failed, not merely that one did.
- `servers/rxnlabel/tests/test_degradation.py::test_a_refusal_by_the_egress_guard_is_counted_as_one`
  — the cause that the `OSError` inheritance would otherwise hide.
- `servers/rxnpredict/tests/test_readiness.py::test_losing_a_predictor_moves_a_series_an_operator_scrapes`
  — a smaller ensemble, visible from a scrape.
- `servers/rxnpredict/tests/test_readiness.py::test_every_predictor_module_hands_its_exception_to_the_registry`
  — the eleven call sites, read as source. Those `except` blocks run at import in a checkout where
  every guarded import fails the same way, so no behavioural test can reach one; measured, dropping
  `exc=exc` from a single module left this server's whole suite green.
