# D-2026-09-12-a-readiness-check-that-does-not-run-the-thing-is-not-a-readiness-check — A readiness check that does not run the thing is not a readiness check

**Status:** accepted · **Date:** 2026-09-12 · **Commit:** wave W25, on top of `285c96d`. **No hash
is written for this pass**, for the reason `D-2026-09-12-a-bypass-that-is-not-in-the-suite-is-not-closed`
§5 gives.

## Context

`/healthz` stopped being a constant 200 some waves ago and every server passes a `readiness=`
callable. Nothing had ever driven one against a *broken* dependency, so which of the seven actually
close the gap they were built for was unmeasured. This pass broke each and read the probe.

| server | what was broken | before |
| --- | --- | --- |
| `props` | `records.csv` mutated | import-time `DatasetError` — the pod never serves (`tools.py` loads at module scope) |
| `chem` | `records.csv` mutated | **503**, naming both hashes |
| `safety` | `rules/rules.yaml` mutated | **503**, naming the table and both hashes |
| `pyexec` | the child made unstartable | **503**, "the pyexec sandbox could not run its readiness probe" |
| `rxnpredict` | `trust_priors.json` mutated | import-time `DatasetError` — the pod never serves |
| **`calc`** | `CHEMCLAW_XTB_ENGINE=xtb` on an image with no `xtb` | **200** |
| **`rxnlabel`** | a namer that imports and raises on every reaction | **200** |
| **`rxnpredict`** | an allow-list naming a predictor this build lacks | **200** |

Three gaps, and each is the *same* shape: the probe checked that something could be **named** rather
than that it could be **done**.

- **`calc`** derives a `calc_version()` and stops. `resolve_backend()` honours an explicit
  `xtb_engine=xtb` without asking whether the binary exists, and `xtb_cli.binary_version()` answers
  `"absent"` rather than raising. Measured, the string came back
  `...opt-GFN2-xTB+xtb+xtb-absent/tblite-0.7.0/rdkit-2026.3.5/h2` — well-formed, and a Chemclaw3
  calculation-cache and calibration-ledger key naming a program that never ran. Those rows become
  unreachable the day the binary arrives and the key moves. `binary_version`'s own docstring argued
  the case on "`resolve_backend()` will therefore never select `xtb`", which holds under `auto` and
  not under the explicit setting; it is corrected in place.
- **`rxnlabel`** checked *construction*: `version._installed` says a distribution is present,
  `mapping.available()`/`naming.available()` say it built, and the two disagreeing is a fault. That
  is right and it is the smaller half. A component that builds and then raises on every reaction
  passes it — and `naming.available()` is only an import, so a namer with a corrupt SMIRKS table
  reported present, answered 200, and labelled a corpus "nothing matched".
- **`rxnpredict`** verified the trust priors and nothing about the *ensemble*, which is what this
  server is. Predictor modules are imported at startup and a failure is recorded in a map; measured
  with `CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS=reaction_t5_v2` against a build without it,
  `/healthz` was 200 and `predict_forward_reaction` raised "no forward predictors are available in
  this deployment" on the first call — a pod in service that could not serve.

## Decision

**Run the thing. And distinguish a permanent defect from a transient one, because in this repository
a readiness failure is a restart.**

Every Deployment here points `readinessProbe` *and* `livenessProbe` at `/healthz`, so an unready
answer does not shed load — it replaces the pod, back into whatever caused it. So:

- `degradation.PERMANENT_CAUSES` = `{egress_refused, failed}` is what a readiness check may act on.
  A NetworkPolicy and an armed guard do not improve under load, and a checkpoint that will not parse
  does not either.
- `resource_exhausted` is **counted, reported in the answer, and never a reason to leave.** A memory
  spike is a property of the moment; a probe that acted on it would turn one busy minute into a
  fleet-wide restart loop. It is visible on `chemclaw_mcp_degraded_total` instead, which is where a
  pod thrashing on memory should show up.
- `not_installed` is not a fault at all. This is the constraint that shapes the whole rule, and the
  dev checkout is the counter-example that enforces it: it carries none of the ML extras, reports
  eleven predictors and two labeller components unavailable, and is working exactly as designed. A
  probe that refused it would make the suite unrunnable and teach everybody to ignore the signal.

Concretely: `rxnlabel`'s probe now maps and names its fixture reaction and refuses on a permanent
cause; `rxnpredict` gains `engine/readiness.py`, which refuses for a predictor unavailable for a
permanent cause and for any name an `ENABLED_*_MODELS` allow-list carries that is not registered;
`calc`'s `_readiness` refuses when the *resolved* backend is `xtb` and the binary is absent.

The allow-list arm is the loudest of the three and is refused whatever the cause: somebody wrote the
name down, and a server that will answer with nothing is a configuration that cannot work.

### What was rejected

**"Unready when the registry is thin."** It is the obvious rule for `rxnpredict` and it is wrong in
exactly the direction that matters — see `not_installed` above.

**Refusing in `xtb_cli.binary_version()` rather than in the probe.** A raise there surfaces as a
failed calculation to whoever happened to ask; a readiness failure names the configuration to fix,
before the pod takes traffic.

**Measuring the two `rxnlabel` components by construction alone and calling the gap closed.** That
is what the previous record did, and it was true of `map_reaction`'s absence and blind to its
failure — a sentence this fleet keeps having to write about itself.

## What keeps it true

- `servers/calc/tests/test_readiness.py::test_an_explicit_xtb_backend_with_no_binary_refuses_traffic`
  — the configuration that derived a key naming a program the image does not carry.
- `servers/calc/tests/test_readiness.py::test_an_explicit_xtb_backend_with_the_binary_present_is_ready`
  and `test_the_default_auto_backend_is_ready_on_an_image_with_no_binary` — both counterfactuals. The
  second is the one that would fail if the refusal were written against the binary rather than the
  resolved backend, which would take every dev pod in this fleet unready.
- `servers/rxnlabel/tests/test_degradation.py::test_a_component_that_raises_on_the_probe_takes_the_pod_out_of_rotation`
  — it asserts the premise too: a broken namer still reports `available()`.
- `servers/rxnlabel/tests/test_degradation.py::test_a_pod_that_ran_out_of_memory_is_counted_and_left_alone`
  — the restart-storm arm, driven with a `MemoryError`.
- `servers/rxnlabel/tests/test_readiness.py::test_an_uninstalled_component_is_ready_and_not_a_failure`
  and `test_an_installed_component_that_will_not_construct_is_unready` — the construction half, kept.
- `servers/rxnpredict/tests/test_readiness.py::test_a_checkout_with_no_extras_installed_is_ready`
  — the counter-example the rule is shaped around.
- `servers/rxnpredict/tests/test_readiness.py::test_a_predictor_this_image_carries_and_broke_is_unready`
  and `test_an_egress_refusal_during_load_is_unready`.
- `servers/rxnpredict/tests/test_readiness.py::test_an_enabled_model_this_build_does_not_have_is_unready`
  and `test_an_enabled_model_that_is_registered_is_ready` — the second is what makes the first about
  the name rather than about the variable.
- `servers/rxnlabel/tests/test_readiness.py::test_the_app_wires_the_probe_in` — a readiness callable
  nothing passes to `connector_app` is a control that does not exist.
