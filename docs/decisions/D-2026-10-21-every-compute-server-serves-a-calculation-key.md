# D-2026-10-21-every-compute-server-serves-a-calculation-key — a server whose tools compute serves `calculation_key` for each of them, derived by the kit, naming the engine build

**Status:** proposed · **Date:** 2026-10-21

## Context

[`Chemclaw3`] never recomputes a persisted result (its D-011). For that it needs, before
computing, a key that names everything the result depends on. Only `calc` provides one:
- its `calculation_key` tool returns `CalculationIdentity` with `calc_type`, `calc_version`,
  `input_hash` and `params_hash`;
- `calc_version` is `method + engine + backend_version`.

`CalculationKey.build` lives in `servers/calc/engine/key.py`, a deliberate copy of Chemclaw3's,
pinned by `servers/calc/tests/test_key_contract.py`.

A second compute server would have to copy it again, or skip keying. So would `qm`, proposed in
D-2026-10-21-a-prepare-and-parse-server-is-not-a-dft-tier. And an engine running on two clusters
with different builds would produce the same key for different numbers.

## Options

1. **calc-only** (status quo). Each new compute server invents its own keying, or has none.
2. **Kit-owned derivation and a manifest obligation.**
   - `mcp_server_kit/keys.py` owns `CalculationKey.build` and `stable_hash`.
   - `chemclaw_contracts.compute` owns `ComputeIdentity` (today's `CalculationIdentity`, kept as
     an alias for one minor version) and `KeyedResult`.
   - A manifest declaring `compute: true` obliges the server to answer `calculation_key` over
     `/rpc` with a non-null key, or a stated caveat, for every compute tool.
3. **Key in Chemclaw3 from the arguments.** Chemclaw3 cannot see what an engine build changes, so
   its key would be well-formed and stale.

## Decision

**Option 2.**
- `calc` moves onto the kit's function with byte-identical keys.
- For an engine that runs off the serving pod, `calc_version` also includes the backend image
  digest.
- A contracts **minor** bump: the change only adds things.

## Consequences

- `servers/calc/engine/key.py` keeps only what is calc-specific. The duplicate builder goes.
- A fleet test calls `calculation_key` for every tool of every `compute: true` server and refuses
  a null key without a caveat. It arrives with the change.
- Chemclaw3's cache becomes backend-agnostic (its D-2026-10-21-every-compute-primitive-serves-a-calculation-key).

## What keeps it true

Today, the cross-repository key contract:
`servers/calc/tests/test_key_contract.py::test_the_flat_key_format_is_the_one_chemclaw3_parses` and
`servers/calc/tests/test_key_contract.py::test_build_folds_the_epoch_into_params_and_nothing_else`.
The fleet-wide obligation's own test comes with the change that implements it.

[`Chemclaw3`]: https://github.com/8fqycwdt8v-oss/Chemclaw3
