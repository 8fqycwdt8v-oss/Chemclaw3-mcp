# D-2026-09-18-a-corpus-that-cannot-be-read-is-a-probe-s-answer-not-an-import-error — A corpus that cannot be read is a probe's answer, not an import error

**Status:** accepted · **Date:** 2026-09-18 · **Commit:** the `props` compare bound and
`rxnpredict`'s settings load. Closes the `docs/BACKLOG.md` row that posed the choice; applies
`D-2026-09-12-a-readiness-check-that-does-not-run-the-thing-is-not-a-readiness-check` one step
earlier, to the process that never gets far enough to run one.

## What was measured

Four servers in this fleet vendor a corpus. Driven on this commit by appending one newline to each
corpus and importing the server's `tools` module, then asking `/healthz` under `TestClient`:

| server | corpus | `import <pkg>.tools` | `/healthz` |
| --- | --- | --- | --- |
| `chem` | `records.csv` | ok | 503, the file and both hashes |
| `safety` | `rules.yaml` | ok | 503, the table named and both hashes |
| `props` | `records.csv` | **`DatasetError`** | never reached |
| `rxnpredict` | `trust_priors.json` | **`DatasetError`** | never reached |

Neither of the two is *dangerous* — a pod that cannot start serves nothing. What is lost is the
reason: a kubelet sees `CrashLoopBackOff` and the two hashes are in a container log, where the other
two servers put them in a probe body an operator reads without a shell on the pod.

The two mechanisms are different and were diagnosed separately.

**`props`** computed its one input bound at module scope:

```python
MAX_COMPARED_SOLVENTS = len(records.all_solvents())
```

`records.all_solvents()` goes through `load_dataset`, so the bound was *verified* corpus bytes and
the import was a checksum check. The comment above it argued the derivation on staleness — a number
computed from the table cannot disagree with the table when a row is added — which is true and is
not the property that was in conflict.

**`rxnpredict`** did it through a settings object. `tools.py` calls `register_requested()` at module
scope, `register_requested` calls `get_settings()`, and `get_settings()` eagerly populated
`model_trust_priors_by_class` from the vendored `trust_priors.json`. The traceback on this commit
ran `tools.py:113 → base_doubles.py:152 → config.py:173 → trust_priors.py:70 → datasets.py:226`.
`register_requested` wanted two environment strings and got a checksummed file with them.

Two claims in the tree were false because of it, in the reassuring direction. `app.py`'s
`_readiness` docstring said the priors load happens "inside the *first tool call*, not at import"
and cited `config.py`'s `get_settings` docstring as the authority; both were describing a
`get_settings()` that no longer matched its callers. `props`'s `_readiness` docstring called its own
module-scope load an accident and said "deleting that line would have silently removed" a readiness
check — which was the right worry about the wrong line, since `props` has had a real `readiness=`
callable since it was written.

## What was decided

**A corpus is loaded lazily on every server, and the probe is what fails on a bad one.** Both
servers keep verifying; what changes is *where* the verification's failure lands.

`props`: `MAX_COMPARED_SOLVENTS` is a declared constant, checked against the live table by a test.
Three options were weighed and two rejected outright.

- **Counting rows without verifying the checksum** is the obvious idea and is the worst of the
  three. The bound is part of the tool schema Chemclaw3 advertises — it reaches the agent as
  `maxItems` on `compare_solvent_properties` — so it would derive an advertised contract from a file
  nobody approved, which is the single thing `mcp_server_kit.datasets` exists to prevent.
- **Catching `DatasetError` at module scope and falling back** advertises a bound derived from
  nothing, silently, on exactly the pod whose table is broken.
- **Declaring it and testing it** moves the derivation to where reading the corpus costs a pod
  nothing. The staleness the old comment guarded against becomes a red test naming the number to
  write, instead of a bound that can only be wrong in silence.

`rxnpredict`: the fix is not at the call site, because the call site was reasonable. `get_settings()`
did two things — parse the environment, and load a vendored corpus — and only the first is what a
module-scope caller is asking for. It is now pure `Settings()` construction, and
`Settings.class_priors()` reads the table, cached, behind it: the env override when one is set, the
vendored file otherwise. The three serving readers move to it, and `app.py`'s readiness callable
calls it explicitly, so the probe still *runs the thing* rather than checking it could be
constructed. Fixing the narrower thing — handing `register_requested` its own `Settings()` — would
have left the next module-scope settings read to re-break it with nothing saying so.

Shipped behaviour is unchanged: `trust_priors.json` ships as `{}` on purpose, so
`model_trust_priors_by_class` was already empty after the eager load and is already empty after the
lazy one. What changed is which failure the pod shows.

**What this costs**, stated because it is real: a corrupt corpus is now discovered a few hundred
milliseconds later, by the first probe rather than by the interpreter, and — if a probe were somehow
never run — by the first tool call. That is the arrangement `chem` and `safety` have always had, and
`test_every_server_hands_connector_app_a_readiness_check` is what makes "somehow never run"
unreachable for a server in this fleet.

## What keeps it true

- `tests/test_fleet.py::test_a_corrupt_corpus_is_the_probe_s_answer_rather_than_an_import_error` —
  the fleet-wide half, parametrized over the servers that vendor a corpus, derived from
  `dataset.json` on disk so the ninth one owes the proof the day it ships. It drives a subprocess
  with the dataset loader refusing everything, and asserts the imports survive, `/healthz` answers
  503, and the body names the reason. Driven red before the fix: `props` and `rxnpredict` failed on
  the import, `chem` and `safety` passed.
- `servers/props/tests/test_tools.py::test_the_compare_bound_is_the_size_of_the_table` — the
  declared constant against the live count, equality in both directions. Driven red at 43.
- `servers/props/tests/test_tools.py::test_the_compare_bound_is_the_one_the_tool_schema_advertises`
  — the constant against `maxItems` on the advertised input schema, so the test above is about the
  contract and not about a module-level name. Driven red with `max_length=50` written in by hand.
- `tests/test_fleet.py::test_every_server_hands_connector_app_a_readiness_check` — the existing
  ratchet this one sits behind; a lazy corpus is only safe because every server has a probe.
