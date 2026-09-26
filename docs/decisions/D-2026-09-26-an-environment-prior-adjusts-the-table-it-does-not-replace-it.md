# D-2026-09-26-an-environment-prior-adjusts-the-table-it-does-not-replace-it — An environment trust prior adjusts the table; it does not replace it

**Status:** accepted · **Date:** 2026-09-26

## What was found

`servers/rxnpredict`'s `Settings.model_trust_priors` is a `dict[str, float]` under
`env_prefix="CHEMCLAW_RXNPREDICT_"`, so it is environment-settable, and the environment value
**replaced the whole table**. Re-measured on `origin/main` at 7261c04:
`CHEMCLAW_RXNPREDICT_MODEL_TRUST_PRIORS='{"parrot": 9.9}'` gives `Settings().model_trust_priors ==
{"parrot": 9.9}`, so every other predictor falls to `effective_prior`'s unweighted default of 0.5 —
`reaction_t5_v2` halved from 1.00 — with no error and nothing in the answer to say so. A scientific
behaviour change by environment variable, and not the one the operator asked for.

## What was decided

The row offered two answers: extend `tests/test_fleet.py::_numeric_settings_fields` to
string-parsed container fields, or argue the field in the register. **Neither, and the behaviour
itself is fixed instead**, because the ratchet reads only the files this repository ships: the
realistic way this variable is set — a Helm value, a `kubectl set env`, an overlay — is invisible to
it by construction (the row above it in `docs/BACKLOG.md` says so), and it would have gone on
replacing the table there.

- **A supplied table is merged onto `DEFAULT_MODEL_TRUST_PRIORS`**: the entries it names replace
  those defaults and the rest stand. A full replacement is still expressible by naming every
  predictor, so nothing that was possible before is lost.
- **What the aggregator cannot mean is refused at settings load**: a key that is not a predictor the
  table weights (a typo would set nothing and read as done), a non-number, and a weight that is not
  finite and positive — zero silences a predictor that still reports having voted, and
  `CHEMCLAW_RXNPREDICT_DISABLED_MODELS` is the switch for that.
- `model_trust_priors_by_class` keeps its documented whole-table override of the vendored corpus;
  whether it should merge too is queued in `docs/BACKLOG.md` rather than decided here.

## What keeps it true

- `servers/rxnpredict/tests/test_trust_priors.py::test_an_env_prior_adjusts_the_table_rather_than_replacing_it`
- `servers/rxnpredict/tests/test_trust_priors.py::test_no_env_value_is_the_default_table`
- `servers/rxnpredict/tests/test_trust_priors.py::test_an_env_prior_the_aggregator_cannot_mean_is_refused`
