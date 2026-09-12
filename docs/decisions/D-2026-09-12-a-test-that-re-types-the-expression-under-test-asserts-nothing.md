# D-2026-09-12-a-test-that-re-types-the-expression-under-test-asserts-nothing — A test that re-types the expression under test asserts nothing

**Status:** accepted · **Date:** 2026-09-12 · **Commit:** wave W23 follow-up, on top of `085f636`.
**No hash is written for this pass**, for the reason
`D-2026-09-12-a-bypass-that-is-not-in-the-suite-is-not-closed` §5 gives.

Corrects one sentence of `D-2026-09-12-one-tool-call-is-not-one-thread`, which is otherwise right
and is not edited.

## Context

`D-2026-09-12-one-tool-call-is-not-one-thread` put an admission gate in three servers, each server
copying `servers/chem`'s shape. Every gate behaves correctly — driven, the charge is the fan-out,
the refusal is prompt, the slot follows the work and not the awaiter. What the tests hold is a
different question, and four of the properties that decide whether the gate is worth having were
held by nothing.

## 1. A monkeypatched gate is evidence about the test's `Admission`, not the server's

Every gate test in `rxnpredict` and `rxnlabel` does
`monkeypatch.setattr(tools, "_admission", Admission(n))`. That is correct for exercising behaviour
at a small ceiling, and it means nothing read `tools._admission` at all. Hardcoding
`rxnpredict`'s to `Admission(64)` — 32x its pod's `limits.cpu: "2"`, the environment variable
ignored — passed **203 tests**, `test_the_ceiling_is_the_pods_own_core_count` among them, because
that one compares the *constant* to the Deployment and never touches the gate.

`servers/chem` already had the missing test, and its docstring says exactly what it is for: *"what
makes every monkeypatched ceiling above evidence about the real gate"*. Both servers that copied the
gate omitted the test that makes the copy checkable. It is ported to both, along with chem's
`test_a_gated_tool_still_advertises_its_real_signature`, so `functools.wraps` — without which every
gated tool advertises `(*args, **kwargs)` to the model — is asserted in all three.

## 2. The most-argued property of `rxnpredict`'s gate had no test at all

`_forward_ensemble_slots` reads `_forward_predictors(None)` — the deployment's enabled list —
rather than the caller's `models`, and the record argues it: *a charge a caller can lower is a
ceiling a caller can walk past*. The execution path narrows on `models` and the charge does not,
which is the asymmetry that makes the walk-past possible. **No test in the file ever passed a
`models` argument.** Mutating `_admitted` to take its cost from `kwargs.get("models")` — reinstating
exactly the walk-past the record argues against — left all 11 tests in the file and all 112 in the
server green.

The probe drives it by keyword and positionally, and the first arm is the one that matters: FastMCP
unpacks validated arguments as `**kwargs`, so a mutation reading `kwargs` is invisible to a test
that only calls positionally. Both halves are asserted — that the execution path *does* narrow, and
that the charge does not — because a version that narrowed neither would satisfy an assertion about
the charge alone while having nothing left to protect.

## 3. Three tests compared a re-typed copy of the expression under test to itself

`servers/rxnlabel/tests/test_admission.py::test_both_bounds_are_environment_variables_and_not_constants`
claimed in its own docstring to be *"asserted by reading the module twice with different
environments rather than by reading its source"*, and did neither. It set two variables and compared
`int(os.environ.get("CHEMCLAW_RXNLABEL_MAX_BATCH", "500"))` — the expression under test, re-typed
into the test — to the numbers it had just set. `tools.MAX_BATCH` was never read. Replacing the
module's read with a hardcoded `MAX_BATCH = 500` left **209 tests** green.
`rxnpredict`'s and `chem`'s are the same shape; chem's kept the re-typed copy in a helper named
`_configured_ceiling()` *in the test file*, which reads as a shared derivation and is a second
transcription.

**`tests/test_fleet.py`'s inventory cannot catch this, and the reason generalises.**
`numeric_env_bounds()` derives the set of movable bounds *from the source*, so removing a read does
not fail the ratchet — it silently shrinks what the ratchet covers. A check derived from the thing
it is checking agrees with itself in exactly the case it exists for.

`mcp_server_kit.testing.reimported` executes a module's own source under the environment in force
now, as a throwaway module object. `importlib.reload` was rejected: it rebinds the entry in
`sys.modules`, so every module that did `from ...tools import server` at import keeps the old object
while new callers get a different one, and the test's side effects outlive it.

**A helper that *reads* the variable was rejected for a sharper reason**, and it is the reason the
three fixes are three explicit reads rather than one shared function:
`tests/test_fleet.py::test_the_bound_scan_sees_both_configuration_mechanisms` pins that a read
through a helper is deliberately **not** followed by the scan. Converting these bounds to a shared
`env_int(...)` would remove every one of them from the fleet ratchet — trading a DRY win for the
coverage loss this record is about.

## 4. A constant compared to itself pins nothing

`sessions.AT_CAPACITY_STATUS` was compared to itself by every one of the nine session-ceiling
tests; setting it to `200` left all nine green, so the status a client reads to tell a full pod
from a served handshake was unpinned while looking thoroughly asserted. The same file already
pinned the JSON-RPC code as a literal `-32000`, so the pattern was known and applied to one of the
two channels. Both are a contract with a caller this repository does not own, and both are now
literals. `D-2026-09-12-a-ceiling-read-before-the-mint-is-a-ceiling-a-burst-walks-past` §4 covers
the `Retry-After` value beside it.

## 5. A correction to `D-2026-09-12-one-tool-call-is-not-one-thread`

That record says `render_structure` *"holds the interpreter for 97 ms"*, and
`servers/chem/tests/test_admission.py` shipped the same sentence.
`servers/chem/tests/test_depiction_bound.py` already recorded, in the same tree, that the 241-atom
peptide behind that figure is **refused outright** by `MAX_DEPICTION_CHARS`. Re-measured here: that
peptide renders to 165,030 characters against a 50,000-character limit and never draws, and the
worst *legal* depiction — the 76-atom peptide that file pins — is **5.66 ms** (aspirin: 0.64 ms).

The gate's derivation is unaffected and conservative in the safe direction, as
`test_depiction_bound.py` already says. What was wrong is a number in prose, written into a new
record by the commit that cites the rule against doing it. The test's sentence now transcribes no
figure and points at the file that measures one.

## What keeps it true

- `servers/rxnpredict/tests/test_admission.py::test_the_shipped_gate_enforces_the_shipped_default`
  and `servers/rxnlabel/tests/test_admission.py::test_the_shipped_gate_enforces_the_shipped_default`
  — §1, the module-level gate the server actually serves behind, beside chem's original.
- `servers/rxnpredict/tests/test_admission.py::test_a_gated_tool_still_advertises_its_real_signature`
  and `servers/rxnlabel/tests/test_admission.py::test_a_gated_tool_still_advertises_its_real_signature`
  — §1, schema preservation through the decorator, in the two servers that omitted it.
- `servers/rxnpredict/tests/test_admission.py::test_the_charge_does_not_follow_the_callers_models_argument`
  — §2. It fails against the mutation that reinstates the walk-past, by keyword and positionally.
- `servers/rxnlabel/tests/test_admission.py::test_both_bounds_are_environment_variables_and_not_constants`,
  `servers/rxnpredict/tests/test_admission.py::test_the_ceiling_is_an_environment_variable_and_not_a_constant`
  and `servers/chem/tests/test_admission.py::test_the_ceiling_is_an_environment_variable_and_not_a_constant`
  — §3, each now reading the value off the module under two environments. The names are unchanged
  on purpose: a merged record cites one of them, and the name was never the defect.
- `packages/mcp_server_kit/tests/test_session_ceiling.py::test_the_refusals_status_and_retry_interval_are_what_a_client_is_told`
  — §4, the literal.
- `servers/chem/tests/test_depiction_bound.py::test_the_worst_legal_depiction_still_costs_what_the_ceiling_was_derived_from`
  — §5: the measurement the ceiling is actually derived from, which was right all along.
