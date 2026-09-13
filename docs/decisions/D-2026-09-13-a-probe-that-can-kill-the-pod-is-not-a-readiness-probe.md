# D-2026-09-13-a-probe-that-can-kill-the-pod-is-not-a-readiness-probe — A probe that can kill the pod is not a readiness probe

**Status:** accepted · **Date:** 2026-09-13 · **Commit:** wave W25 follow-up, on top of `116b8306`.
**No hash is written for this pass**, for the reason
`D-2026-09-12-a-bypass-that-is-not-in-the-suite-is-not-closed` §5 gives: the work merges by squash,
so any hash this session could name is a branch commit `main` will not contain, and
`test_every_commit_the_registers_cite_is_reachable_from_head` refuses one.

## Context

Wave 25 set out to stop silent degradation
(`D-2026-09-12-a-degradation-that-is-not-counted-is-a-degradation-nobody-sees`,
`D-2026-09-12-a-readiness-check-that-does-not-run-the-thing-is-not-a-readiness-check`). A
fresh-context review of the merged result found it had **traded a silently degraded pod for an
outage**, in three drivable ways, and that the rule both records state — "only
`degradation.PERMANENT_CAUSES` may cause an unready answer; a transient resource exhaustion is
counted and left alone" — was implemented in two `if` statements and nowhere else.

Everything below was re-driven in this tree at `116b8306` before anything was changed.

### `PERMANENT_CAUSES` gated two code paths out of seven callables

```
$ grep -rn PERMANENT_CAUSES --include=*.py servers/*/src packages/*/src
servers/rxnlabel/.../engine/readiness.py:122
servers/rxnpredict/.../engine/readiness.py:59
```

All seven servers pass a `readiness=` callable, and every line of those callables that is not one of
those two comparisons raised straight through `connector_app`'s `except Exception` into an
unconditional 503. Three drives:

| # | Driven | Result |
| --- | --- | --- |
| 1a | `rxnmapper` in metadata, `RXNMapper()` raising `MemoryError`, real app | counter books `resource_exhausted`; `resource_exhausted in PERMANENT_CAUSES` is **False**; `/healthz` **503** |
| 1b | `MemoryError` out of the probe's non-optional half | `/healthz` **503**, reason "Unable to allocate array" |
| 1c | one missing checkpoint among eleven *optional* `rxnpredict` predictors | baseline 200 with ten of eleven absent; `classify(FileNotFoundError)` = `failed`, permanent; `/healthz` **503** |

And every Deployment pointed `readinessProbe` **and** `livenessProbe` at `/healthz`
(`periodSeconds: 30`, `failureThreshold: 3`), so each of those 503s was a **kill after ~90 s**. For
1c a restart cannot recreate a missing file: the pod that had been serving ten of eleven predictors
went to `CrashLoopBackOff` and served none.

### The permanent bucket was wider than its own comment

Re-driven in full:

| exception | cause | permanent? |
| --- | --- | --- |
| `EgressForbidden` | `egress_refused` | yes |
| `MemoryError`, `OutOfMemoryError` by type name | `resource_exhausted` | no |
| `RuntimeError("CUDA out of memory")` | `failed` | **yes** |
| `OSError(ENOMEM, "Cannot allocate memory")` | `failed` | **yes** |
| `OSError(EMFILE)`, `OSError(ENFILE)`, `OSError(EAGAIN)` | `failed` | **yes** |
| `OSError(ENOSPC)`, `TimeoutError`, `asyncio.CancelledError` | `failed` | **yes** |
| `ImportError`, including `libcudart.so.11: cannot open shared object file` | `not_installed` | no |

`CAUSE_RESOURCE_EXHAUSTED`'s own comment reads "the process ran out of something it can get
back: memory" and `ENOMEM` — the kernel saying precisely that — sat in the permanent bucket, as did
a pod at its descriptor ceiling and a pod at its thread ceiling.

### Three more, each a test that passed with its subject broken

- **`servers/rxnpredict/tests/test_readiness.py::test_every_predictor_module_hands_its_exception_to_the_registry`**
  is the test the earlier record names as keeping the `exc=exc` invariant true, and it does not. Its
  assertion is `assert not missing` over a collected list. Driven: `exc=exc` → `exc=None` in
  `megan.py` left **295 passed** (and `None` takes `mark_unavailable`'s documented
  `CAUSE_NOT_INSTALLED` default, so a corrupt checkpoint reads as an extra nobody installed);
  deleting the `mark_unavailable(...)` call outright also left **295 passed**, because an emptiness
  check over a collected list is satisfied by *zero* matching call sites. Its docstring claimed to
  cover "the eleven call sites" and nothing asserted eleven.
- **`calc` did not close the `xtb-absent` key.** `app._readiness` gates on
  `resolve_backend() == "xtb"`, while `xtb_spec._FIXED_BACKEND` pins the `atomic` and `surface` tasks
  to the binary *regardless of configuration*. Driven under the shipped default
  (`CHEMCLAW_XTB_ENGINE` unset, no `xtb` on PATH): `/healthz` **200** and `calculation_key` answering
  `xtb.atomic@GFN2-xTB+xtb+xtb-absent/tblite-0.7.0/rdkit-2026.3.5/h2:...` for 2 of 17 tools — a
  well-formed Chemclaw3 cache and ledger key naming a program the pod does not carry. The compute
  calls do raise, so the harm is narrower than the earlier record's framing, and
  `servers/calc/tests/test_readiness.py::test_the_default_auto_backend_is_ready_on_an_image_with_no_binary`
  asserted that exact configuration ready — so the suite pinned it.
- **`verify_labeller` was `lru_cache(maxsize=1)`**, and its docstring argued the cache was safe
  because "`lru_cache` does not cache exceptions, so a broken pod is re-probed and stays 503" — true
  of a pod broken at *startup*, false of one that breaks later. Driven on the real app through one
  client: three probes 200, the namer then raising on every reaction, probe four **200**.

### And one thing `record` did that made it the fault

`record` raised `ValueError` for a cause outside `CAUSES`, from inside the `except` block whose job
is to degrade gracefully, and `connector_app` passes `ValueError` to the model verbatim. Driven with
`classify` patched to return a fifth cause: `map_reaction` raised instead of degrading and a
chemist's answer became a sentence about Prometheus labels. Unreachable today, because every call
site passes `classify(...)` or a module constant; uncovered by anything.

## Decision

**Liveness gets its own route, the classification funnel moves into `connector_app`, and a pod is
unready for a capability it cannot deliver rather than for the size of an optional set.**

### 1. `/livez`, and the probe-coupling question answered explicitly

`connector_app` serves `GET /livez` (and `/livez/`), in `auth.OPEN_PATHS`, answering 200 and
consulting *nothing* — no corpus, no model, no backend, no lock. Every Deployment's `livenessProbe`
points at it; `readinessProbe` stays on `/healthz`.

**The argument, and the case against.** The case for sharing one route is that there is one question
— "can this pod work" — and one answer is simpler than two. It is wrong because kubelet does two
different things with the answer: a readiness failure removes the pod from its Service's endpoints
and is undone by the next passing probe, while a liveness failure replaces the container. So every
dependency `/healthz` consults became a restart trigger, and for the dependencies this fleet actually
has — an optional checkpoint, a transformer's weights, a corrupt table — a restart is either useless
(1c: a missing file) or harmful (1a/1b under sustained node memory pressure: a restart arriving back
into the same pressure).

**What it costs, stated because it is a real behavioural change.** A pod that is permanently unready
— a corrupt checkpoint, say — is no longer restarted automatically. It sits at `0/1 Ready` with the
reason on `/healthz`, which an operator can read and act on, where before it sat in
`CrashLoopBackOff` with the reason in a container log that rotates. The class of fault a restart
genuinely fixes is a wedged process, and `/livez` still catches exactly that, because uvicorn accepts
connections only once the lifespan has completed and the MCP session manager is running.

### 2. The funnel is `connector_app`'s, not each callable's

`/healthz` classifies whatever the callable raised. A cause in `PERMANENT_CAUSES` answers 503 with
the redacted reason, as before. Anything else answers **200** with `degraded` naming the cause, no
`datasets` key, and one increment on
`chemclaw_mcp_degraded_total{component="readiness"}` — which is the whole of what the rule offers in
exchange for leaving the pod in service.

Here rather than in seven callables because, stated per callable, it was implemented in two of them.
An eighth server inherits it with nothing to write.

**And the transient/permanent asymmetry is now decidable rather than a matter of taste.** With
liveness decoupled, a cause wrongly sorted *into* `resource_exhausted` leaves a broken pod serving,
while one wrongly sorted out of it sheds traffic for one probe interval and is reversed by the next
passing probe. The first is the expensive mistake, so the resource branch stays narrow and explicit:
two type names plus `errno in {ENOMEM, EMFILE, ENFILE, EAGAIN}`. `TimeoutError` and
`asyncio.CancelledError` carry no errno and stay `failed` on purpose; `RuntimeError("CUDA out of
memory")` stays `failed` because matching its message is the control `degradation.py`'s own docstring
refuses to write.

### 3. What makes a pod unready is a capability, never a thin optional set

- **`rxnpredict`** refuses when no predictor *of a kind* is registered **and** a permanent cause took
  one — which is the measured defect the probe exists for (`predict_forward_reaction` raising "no
  forward predictors are available in this deployment" while `/healthz` answered 200) — or when an
  `ENABLED_*_MODELS` allow-list names a predictor that is not registered, because somebody wrote the
  name down. One broken predictor among eleven optional ones is a **degradation**: counted, named in
  `list_available_models`, and an alert on the counter. Both cases the earlier record cites still
  refuse, because this checkout registers no predictor of either kind; the counterfactual a thin
  ensemble needs — broken beside working — is now a test.
- **`calc`** stays ready on an image with no `xtb` binary, and the two tools that cannot be served
  refuse *at the point of asking* as well as at the point of computing. `engine/identity.py` calls
  `xtb_atomic.require_binary_backend` for `compute_atomic_descriptors` and
  `compute_surface_potential`, exactly as it already called `crest_search.require_crest` for the two
  CREST searches. That precedent is the one `xtb_atomic.atomic_inputs`'s docstring *cited* while
  doing the opposite ("this derives a key even where no binary is installed, which is the same thing
  the two CREST searches do" — they call `require_crest`), and it is the rule `identity.py`'s own
  docstring already stated: "the probe refuses precisely where the calculation would". Sharing the
  compute path's own function rather than writing a second refusal also closes the open-shell case,
  which would otherwise key as tblite here and raise there.

  **This also closes the partial-capability question** the BACKLOG row asked about CREST, and in the
  direction CREST already implemented: a pod that can serve fifteen of seventeen tools is ready, and
  the two it cannot serve refuse by name at both surfaces. A readiness gate on the binary would take
  every dev pod in this fleet out of rotation for a program no dev image carries.

### 4. `rxnlabel`: the cause is carried, a transient construction is retried, the verdict expires

`mapping._mapper` stores the cause it classified (`construction_failure()`) instead of discarding it,
and retries a transient failure after `CONSTRUCTION_RETRY_SECONDS`. `naming._namer` records a
construction failure the same way, which it did not before — so two components behind one probe no
longer report differently about the same kind of fault.

**The construction branch still refuses whatever the cause, and that is a deliberate departure from
the review's prescription.** Applying `PERMANENT_CAUSES` there would answer 200 for an installed,
unbuilt mapper — and `version._component` then reads `available()` False and stamps every row
`mapper@absent`, byte-identical to a deployment that never installed one, which is the stamp defect
the degradation record exists to end. A pod in that state writes permanently indistinguishable rows
for as long as it serves. So it sheds traffic; what made that verdict dangerous is §1, not the
verdict. The cause decides something else: whether the refusal can be lifted without a restart, which
before the retry it could not — `_TRIED` latched on the first attempt.

`verify_labeller` caches its verdict — the datasets, the refusal sentence, **or the exception the
labelling path raised** — for `VERDICT_TTL_SECONDS` (60 s, six probe intervals). Expiring the success
is what catches a component that broke later; caching the other two is what bounds the one expensive
thing this probe does, an **unadmitted** RXNMapper forward pass, which on the failure path ran once
per probe interval for ever, since `connector_app`'s five-second memo is shorter than the ten-second
probe period. The probe deliberately does **not** take an `engine/admission.py` slot: a full pod
would then refuse its own probe and answer 503, which is the same argument `connector_app` already
makes for giving readiness its own one-thread pool.

**The labelling path's raises are re-raised rather than classified here**, which is why the exception
is in that cache at all. A first draft of this pass caught them in `readiness._probe`, classified,
counted under a `labelling_path` component and returned ready — and that is §2's decision written a
second time, in a second place, with a second component label and a `/healthz` body that said plain
`ok` where the kit's funnel says `degraded`. One funnel. What this module still owns is the
*cause*-shaped arms, where there is no exception to classify: a `MapResult.failure` and a
`Naming.failure` are already causes, counted by the module that produced them.

### 5. `record` clamps; the hard assertion is in the suite

An unrecognised cause is logged at ERROR and counted as `CAUSE_FAILED`; an unregistered component as
`UNKNOWN_COMPONENT`. Neither mints a series for the unclamped string, so the clamp is still a clamp.
`component` is now declared through `degradation.register_components`, closing the asymmetry where
`cause` was a closed set and `component` was the habit of spelling it as a source constant — driven,
`record(component='hostile"}\n fake_metric 99', ...)` minted that series, though no caller could
reach it.

### 6. The counted assertions, and two this pass nearly shipped vacuous

Three tests now assert a **count** where an emptiness check stood, because this family has shipped
ten tests across four waves that passed with their subject broken and two of them were in this wave.
`RECORD_CALL_SITES` caught its own author mid-commit: written as 5, the test named the sixth site,
and the final figure is 8 after `rxnlabel` gained two.

**Mutation-checking this pass's own tests caught two more of the same species, and the shape is worth
naming because it is not the emptiness check.** A test that *patches the constant it then relies on*
asserts the mechanism and nothing about the shipped number:

- `VERDICT_TTL_SECONDS = 1e9` — `lru_cache`'s behaviour restored under another spelling — left the
  whole `rxnlabel` suite green, because the expiry test sets the TTL to zero itself. The fix is a
  two-sided bound read out of this server's own `deployment.yaml`: at least one readiness period, or
  the fixture's forward pass runs on nearly every probe; at most twelve, or a component that broke
  keeps serving for minutes.
- reverting `discover_predictors`'s catch-all to the module short name left the whole `rxnpredict`
  suite green, including the map-agreement test — because in a checkout where every module imports
  (the guards are *inside* them) the catch-all never fires. The fix drives it, by making one module
  unimportable.

### What was rejected

**Acting on `PERMANENT_CAUSES` in `rxnlabel`'s construction branch**, as the review proposed. It is
the obvious reading of the rule and it reintroduces `mapper@absent` on a broken pod — §4.

**Dropping `PERMANENT_CAUSES` from readiness entirely once `/livez` exists.** Tempting, and it would
be simpler: with no kill attached, shedding traffic on any probe failure is cheap and reversible. It
is declined because `rxnlabel`'s probe runs a transformer forward pass, which is *heavier* than most
of the traffic it gates — a probe failing on an allocation is evidence about the probe, not about the
calls — and because a fleet-wide memory spike would take every endpoint out of every Service at
once, turning a degraded capability into an absent one.

**A per-tool-call wall clock or an admission slot for the probe.** Both are controls that read as one
and are not; `CLAUDE.md` already argues the first, and the second makes a busy pod fail its own
probe.

**Classifying `rxnpredict/engine/cache.py`'s canonicalisation fallback and
`rxnlabel/engine/mapping.py`'s `inference_threads` fallback.** `CLAUDE.md` claimed "every path in
this fleet that catches an exception and answers anyway now classifies it", which is false — those
two catch and answer. Neither answers with a *component's contribution missing*: one uses an
uncanonicalised cache key, the other under-charges the admission ceiling. Counting them on
`chemclaw_mcp_degraded_total` would publish a series about nothing missing, so the sentence is
narrowed to what is true and names both.

**Suppressing paths from the 503 body.** `redact_secrets` scrubs credentials and not paths, so
`FileNotFoundError(2, 'No such file', '/mnt/models/megan/model.ckpt')` reaches an unauthenticated
route verbatim. Kept: the reason is the entire value of the probe body — a generic 503 recreates the
constant-answer defect one level up — the route is cluster-internal behind a default-deny
NetworkPolicy, and the same body already publishes the dataset names, versions and build revision. A
mount path is not a secret, and the one thing that is goes through the redactor.

## What keeps it true

- `packages/mcp_server_kit/tests/test_readiness.py::test_only_a_permanent_cause_answers_unready`
  — the funnel, parametrized over `degradation.CAUSES` and asserted against `PERMANENT_CAUSES`
  membership rather than against transcribed status codes, driven over the served route.
- `packages/mcp_server_kit/tests/test_readiness.py::test_every_cause_reaches_this_funnel`
  — that the parametrization above *is* `CAUSES`, so a fifth cause cannot arrive with no decision.
- `packages/mcp_server_kit/tests/test_readiness.py::test_the_transient_verdict_is_counted_for_a_scrape`
  — leaving a pod in service is only defensible if the degradation is visible.
- `packages/mcp_server_kit/tests/test_readiness.py::test_livez_answers_while_healthz_refuses`
  — the decoupling, including the trailing-slash spelling and that `/livez` runs no readiness check.
- `tests/test_deploy_shape.py::test_liveness_and_readiness_do_not_share_a_route`
  — every Deployment, both paths and their inequality, against literals a kubelet would read.
- `packages/mcp_server_kit/tests/test_auth.py::test_every_probe_route_is_open`
  — parametrized over `OPEN_PATHS`; a liveness probe refused with 401 kills the container.
- `packages/mcp_server_kit/tests/test_degradation.py::test_a_resource_errno_is_not_a_broken_checkpoint`
  — the four errno values, with `ENOSPC` as the counterfactual that stays permanent.
- `packages/mcp_server_kit/tests/test_degradation.py::test_a_refusal_is_still_sorted_before_the_errno_branch`
  — the new branch is an `OSError` branch, which is what `classify`'s order exists for.
- `packages/mcp_server_kit/tests/test_degradation.py::test_an_unclamped_cause_is_refused_rather_than_published`
  — kept under the name the earlier record cites, because what is refused is still the *label*: it now
  asserts the log, the `failed` fallback, and that the unclamped string minted no series.
- `packages/mcp_server_kit/tests/test_degradation.py::test_an_unregistered_component_is_clamped_the_way_a_tool_name_is`
  — the other half of one label rule.
- `packages/mcp_server_kit/tests/test_degradation.py::test_classify_cannot_answer_outside_the_clamped_set`
  and `test_every_call_site_derives_its_cause_rather_than_writing_one` — the hard assertion `record`
  gave up, the second as a counted AST check over both servers and the kit.
- `packages/mcp_server_kit/tests/test_degradation.py::test_torch_really_names_its_oom_the_way_this_matches_it`
  — the half a locally-defined double cannot assert; skipped with the reason where torch is absent.
- `servers/rxnpredict/tests/test_readiness.py::test_every_predictor_module_hands_its_exception_to_the_registry`
  — rewritten: the count, and that `exc=` is the name the enclosing `except ... as <name>` binds.
  Both mutations that left the merged version green now fail it.
- `servers/rxnpredict/tests/test_readiness.py::test_a_broken_predictor_beside_a_working_one_is_ready`
  and `test_a_kind_with_nothing_left_and_something_broken_is_unready` — the pair 1c needed.
- `servers/rxnpredict/tests/test_readiness.py::test_the_module_map_agrees_with_the_registry_names`
  — the module→name map against the predictor classes' own names, which is what keeps one predictor
  from being counted under two component names.
- `servers/rxnlabel/tests/test_readiness.py::test_an_unbuilt_component_is_unready_whatever_the_cause_said`
  and `test_a_transient_construction_failure_is_retried_rather_than_latched` and
  `test_a_permanent_construction_failure_is_not_retried` — §4's three claims.
- `servers/rxnlabel/tests/test_readiness.py::test_a_component_that_breaks_after_a_good_probe_stops_reporting_ready`
  — the `lru_cache` defect, driven through `/healthz`.
- `servers/rxnlabel/tests/test_readiness.py::test_the_verdict_window_is_bounded_by_the_probe_cadence_this_server_declares`
  — the shipped *number*, which the test above patches away; both directions, against the cadence in
  `deploy/deployment.yaml`.
- `servers/rxnpredict/tests/test_readiness.py::test_the_catch_all_files_a_broken_module_under_its_registry_name`
  — the one path the module map exists for, driven by making a module unimportable.
- `servers/rxnlabel/tests/test_readiness.py::test_a_transient_failure_of_the_labelling_path_keeps_the_pod_in_service`
  and `test_a_permanent_failure_of_the_labelling_path_sheds_traffic` — 1b, in both directions.
- `servers/calc/tests/test_calculation_key.py::test_the_tools_that_need_a_binary_refuse_rather_than_key`
  — both directions, so adding a tool to `NEEDS_A_BINARY` to silence a failure fails instead.
- `servers/calc/tests/test_readiness.py::test_the_two_binary_only_tools_do_not_key_on_a_ready_pod`
  — ready *and* no `xtb-absent` key, asserted together because separately each is satisfiable by the
  wrong fix.
