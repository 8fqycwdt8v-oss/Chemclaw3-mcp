# D-2026-09-15-a-server-with-nothing-to-load-still-has-something-to-verify — A server with nothing to load still has something to verify

**Status:** accepted · **Date:** 2026-09-15 · **Commit:** the `thermalsafety` server. Supersedes
nothing; it applies `D-2026-09-12-a-readiness-check-that-does-not-run-the-thing-is-not-a-readiness-check`
to the first server in this fleet that has no corpus, and records why that server's first version
was wrong to claim an exemption.

## Context

`MODULES.md` has carried a `thermalsafety` row at port 8851 since tranche 1 was planned: the
arithmetic between a calorimetry report and a scale-up decision — adiabatic temperature rise, MTSR,
TMR_ad, the Stoessel criticality class, heat-removal capacity, an oxygen-balance screen. It closes a
gap Chemclaw3's own refusal prompt names in the present tense: *no calorimetry, heat- or
mass-transfer, mixing or addition-rate model, so a computed reaction enthalpy is never a process
heat load, an adiabatic rise, a jacket duty or a safe addition rate.*

It is also the first server here whose dependency closure is the MCP transport and **nothing else**.
`props` reads a vendored CSV, `chem` and `safety` read corpora, `calc` loads tblite's compiled
parameters, `rxnpredict` loads checkpoints. This one computes from `math` and a seventeen-element
atomic-weight dict written in Python source.

So its `app.py` shipped with no `readiness=` callable, and a test in its own directory asserted that
*absence* deliberately, with an argument that reads well: there is nothing to load, a probe could
only check that a module imports, which the process proved by starting, and D-2026-09-12 is
precisely about probes that pass a component which then fails on every call.

`tests/test_fleet.py::test_every_server_hands_connector_app_a_readiness_check` refused it.

## Measured

Running the fleet invariants against the new server, before anything was argued:

```
FAILED tests/test_fleet.py::test_every_server_hands_connector_app_a_readiness_check[thermalsafety]
E   AssertionError: servers/thermalsafety/src/chemclaw_mcp_thermalsafety/app.py calls
    connector_app without `readiness=`, so /healthz is a constant 200 and this pod takes traffic
    whatever state it is in.
```

That invariant has no exemption list and its own docstring says why: *a server with no probe at all
is the same failure one step earlier.*

The interesting part is that it was right on the **facts** as well as on the policy, and the facts
are what settled it. This server does have a corpus. `engine/oxygen_balance.ATOMIC_WEIGHTS` and the
screening-band table beside it are vendored data that happen to live in a `.py` file rather than in a
CSV, and a transposed digit in one of them is exactly the failure
`servers/props/tests/test_dataset.py` exists to catch — a wrong number in a row nobody looks at
again. Being in Python source makes that harder to introduce and **impossible to checksum**, because
no `dataset.json` names the file and nothing computes its digest.

Driven, with carbon moved by 1 g/mol — less than a typo usually is:

```
oxygen balance for C7H5N3O6 computed -68.68%, published -74.0% (tolerance 0.15)
```

A 5.3-point error in a number that feeds an energetic-hazard screen, on a pod that would otherwise
have answered `/healthz` 200 and served every call.

## Decision

`engine/selftest.py` is the readiness check, and it **runs the thing**: three published oxygen
balances (nitroglycerine, TNT, glucose — spanning every band boundary), the hand-computed adiabatic
rise, and the Semenov root checked against its own defining condition rather than against a literal.
Each value was written down independently of this code, which is what makes agreement evidence about
the table rather than about the probe.

It returns a `Dataset` naming `thermalsafety-constants`, its version, and the SHA-256 of the module
holding the constants — so two pods claiming the same version can be *shown* to serve the same
table, which a version string alone cannot do. `records_path` points at that module, because the
constants are the module; pointing it at a file that does not exist would be the provenance record
that quietly says nothing, one field over.

**The exemption argument is deleted rather than narrowed.** "My server is different" is how an
exemption list starts, and this repository has the same finding one document over: the port table
`CLAUDE.md` deleted, the second declaration `manifests/README.md` forbids. A fleet invariant that
holds for seven servers and is argued away by the eighth holds for nothing.

### Two things this decision does *not* claim

**It is not a claim that arithmetic can rot.** `math.exp` will not drift between builds. What can
differ between the image that was reviewed and the image that is running is the *constants*, and
that is what the probe checks — which is why the digest is published beside the version and why the
failing message names `engine/oxygen_balance.py` rather than saying "self-test failed".

**It is not a general licence to write a probe where there is nothing to verify.** The test that
makes this one honest is the one D-2026-09-12 asks for: `tests/test_server.py` breaks the table and
reads the status. A probe nobody has seen fail is a probe nobody has seen.

## Two smaller decisions recorded here because they were taken in the same commit

**The proposed `sadt` tool ships as `semenov_critical_ambient`.** SADT is *defined* by UN Test Series
H on a specific package in a specific size. A tool computing it from a heat balance would be
shipping a regulatory determination as arithmetic — and a model reading `sadt` in a tool list has
every reason to quote the answer as one. The tool is named for the model it implements; the
disclaimer travels in the `basis` string beside every number, and `tests/test_server.py` asserts that
it survives serialisation, because that is where the model reads it. `MODULES.md`'s row is updated
rather than left proposing the old name.

**`oxygen_balance_screen` takes a molecular formula, not a SMILES.** Accepting a structure would mean
either a silently wrong answer or RDKit in this image for the sake of a division. The parser refuses
parentheses, hydrate dots and charges by name rather than guessing at them: `Ca(NO3)2` read
token-wise is wrong by a factor of two on the element that decides the whole number, with a
plausible-looking answer.

## What keeps it true

- `tests/test_fleet.py::test_every_server_hands_connector_app_a_readiness_check` — the invariant
  that refused the exemption; it now passes for all eight servers.
- `servers/thermalsafety/tests/test_server.py::test_the_readiness_probe_refuses_when_the_atomic_weight_table_is_wrong`
  — the D-2026-09-12 proof: carbon moved by 1 g/mol, `SelfTestFailed` raised naming the table, and
  the probe recovering once it is restored.
- `servers/thermalsafety/tests/test_server.py::test_a_failing_probe_is_a_permanent_cause_and_therefore_answers_503`
  — the other half of the rule, against the kit's own classifier rather than a restatement.
- `servers/thermalsafety/tests/test_server.py::test_healthz_names_the_constants_this_pod_verified`
  — the half a constant 200 does not have.
- `servers/thermalsafety/tests/test_oxygen_balance.py` — seven published compounds, each value
  written from the literature rather than from a run of this code.
- `servers/thermalsafety/tests/test_runaway.py::test_every_stoessel_ordering_produces_its_documented_class`
  — all five classes, as a table, because the classification *is* a table and any single case
  passes an off-by-one in the comparison chain.
- `servers/thermalsafety/tests/test_semenov.py::test_the_answer_satisfies_both_equations_that_define_the_crossover`
  — the tangency verified against its defining condition, with the tolerance derived from the
  solver's own bracket rather than picked.
- `servers/thermalsafety/tests/test_tools.py::test_the_semenov_tool_says_in_its_own_words_that_it_is_not_an_sadt`
  and `test_every_tool_returns_a_basis_that_names_its_model_and_its_assumption` — the disclaimer and
  the `basis` contract, asserted over the set so a *new* tool without one fails.
