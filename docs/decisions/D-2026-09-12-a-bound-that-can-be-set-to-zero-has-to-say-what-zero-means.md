# D-2026-09-12-a-bound-that-can-be-set-to-zero-has-to-say-what-zero-means — A bound that can be set to zero has to say what zero means

**Status:** accepted · **Date:** 2026-09-12 · **Commit:** wave W23 follow-up, on top of `085f636`.
**No hash is written for this pass**, for the reason
`D-2026-09-12-a-bypass-that-is-not-in-the-suite-is-not-closed` §5 gives.

## Context

Wave W23 added three numeric knobs to two servers and one to `mcp_server_kit`. Measured under
`0`, the value an operator reaches for first, they did three different things:

```
MCP_MAX_SESSIONS=0                                  -> no ceiling (a documented escape hatch)
CHEMCLAW_RXNPREDICT_MAX_CONCURRENT_PREDICTIONS=0    -> ValueError at import, naming the number
CHEMCLAW_RXNLABEL_MAX_BATCH=0                       -> the pod starts, and refuses everything
```

The third is the one worth the record. There is no crash, no warning and no failed probe: the pod
comes up, passes readiness, takes traffic, and answers every call with *"0 reactions in one request
exceeds the batch limit of 0"*. `-1` behaves identically. That is a server whose every tool is
broken by one character in a ConfigMap, discoverable only from the model's side.

The second is defensible and badly worded: `Admission.__init__` refuses a ceiling below one and its
message names the *ceiling*, not the variable that set it, so an operator gets a CrashLoopBackOff
and a number whose source they have to guess. A non-integer is worse — a bare
`invalid literal for int()` with no variable anywhere in it.

## The decision

**A bound whose job is to refuse has no "off", and says so at import, naming its own variable.**
`MCP_MAX_SESSIONS` keeps `0 = off`, because turning a *session* ceiling off is a real deployment
choice — it reproduces upstream's unbounded behaviour, which a site may want deliberately. An
admission ceiling of zero and a batch bound of zero are not that; they are "serve nothing", which
nobody chooses on purpose. So the two families differ, and the difference is now argued rather than
accidental: the kit's knob is an on/off switch with a documented off, and a server's bound is a
positive integer or a refusal.

The refusal names the variable, the value it was given, what that would have meant, and the default
it would have had. That is what an operator reading a crash loop can act on.

**This is three explicit checks rather than one shared helper**, for the reason
`D-2026-09-12-a-test-that-re-types-the-expression-under-test-asserts-nothing` §3 records:
`tests/test_fleet.py::test_the_bound_scan_sees_both_configuration_mechanisms` pins that the fleet's
inventory of movable bounds deliberately does **not** follow a read through a helper, so folding
these into `env_int(...)` would take every one of them out of the ratchet. The five remaining raw
bounds in the fleet (`chem`'s three, `safety`'s, `pyexec`'s, and `mcp_server_kit/limits.py`'s two)
are a `docs/BACKLOG.md` row rather than a silent inconsistency.

## The prompt may not state a number this process does not know it still has

`represent_reactions` and `name_reactions` read *"At most `CHEMCLAW_RXNLABEL_MAX_BATCH` reactions
per request (500 by default)"*. A model cannot resolve an environment variable, and a deployment
that lowers the bound leaves the prompt confidently saying 500 — the one place W23's
constant-to-variable refactor made the **prompt** less true than the constant was. FastMCP captures
a tool's description at decoration time, so the live value cannot be templated into it without
moving the prose out of the docstring. The docstrings now describe the *refusal* instead, which
names the limit actually in force; `DEFAULT_MAX_BATCH` moves to `engine/admission.py`, beside the
ceiling it is priced against.

## A refusal the model can act on is a `ValueError`

Pre-existing, found in passing and fixed here because it is the same sentence:
`represent_reaction("not-a-reaction")` raised `IndexError: list index out of range`. The
single-reaction tools take `[0]` of a batch that deliberately **drops** what it cannot read — the
batch form is lenient on purpose, because a corpus drain wants the rows it could label rather than
one bad row failing ten thousand good ones. `IndexError` is not the family `connector_app` passes
through, so the model was handed an opaque `error_id` and told a fault had occurred, rather than
that its own input was malformed.

## What keeps it true

- `servers/rxnlabel/tests/test_admission.py::test_a_bound_set_to_nothing_refuses_at_import_and_names_the_variable`
  — both of that server's bounds, at `0` and `-1`, asserted on the variable's own name appearing in
  the message.
- `servers/rxnpredict/tests/test_admission.py::test_the_ceiling_set_to_nothing_refuses_at_import_and_names_the_variable`
  — the same for the ceiling that crashed without naming itself.
- `packages/mcp_server_kit/tests/test_session_ceiling.py::test_the_ceiling_is_an_environment_variable_and_zero_turns_it_off`
  — the other half of the rule: the one knob where `0` is a supported configuration.
- `servers/rxnlabel/tests/test_tools.py::test_a_string_that_is_not_a_reaction_is_refused_in_the_callers_terms`
  — the `ValueError`, over both single-reaction tools and three malformed inputs.
