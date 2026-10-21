# D-2026-10-21-a-prepare-and-parse-server-is-not-a-dft-tier — a server may prepare engine inputs and parse engine outputs for a calculation that runs elsewhere, while running no engine itself

**Status:** proposed · **Date:** 2026-10-21

## Context

The fleet's rule is "No DFT tier: no tool shelling out to ORCA/Psi4/Gaussian/NWChem or calling a
hosted QM API". The rule exists because such a tool would bring an engine's dependency closure, an
unbounded runtime and, for a hosted API, request-time egress into a stateless, egress-free pod.

[`Chemclaw3`]'s proposed D-2026-10-21-long-running-compute-runs-on-a-backend-seam brings method
escalation back on a compute backend seam:
- a driver in a Chemclaw3 worker submits to the site's scheduler, polls it, fetches the outputs
  and cancels;
- one external-job workflow there owns the durability.

That design needs two stateless steps, each a pure function of its arguments. Neither can live in
Chemclaw3, where the physics does not belong (D-2026-08-16 on the Chemclaw3 side):
- turning a method, structure and settings into engine input files plus a cache key;
- turning engine output files into typed results.

## Options

1. **Keep the rule as written.** The prepare and parse steps then have no home; they would land in
   Chemclaw3's orchestration code against its own layering.
2. **A `qm` server limited to prepare and parse.**
   - `prepare_input` returns input files and a `calculation_key`.
   - `parse_output` returns a typed result: energies, gradients, frequencies, thermochemistry,
     convergence flags and the class of any engine error.
   - No engine binary is in the image, nothing is executed, and the egress guard, the static scan
     and the default-deny NetworkPolicy are unchanged.
3. **Run engines in the fleet.** This is the tier the rule declines. The cost it names is real: a
   DFT job is hours of CPU, not a request.

## Decision

**Option 2.**
- "No DFT tier" stays.
- The rule's wording narrows to "no tool that runs a QM engine or calls a hosted QM API".
- A server that only prepares and parses is a primitive like any other: stateless, keyed and bounded.
- `qm` claims a free port in `MODULES.md` (8866) in the PR that adds it.

## Consequences

- `servers/qm` ships parsers tested against committed engine outputs, and `dataset.json` metadata
  for any reference corpus it reads.
- `calc_version` for a `qm` key includes the engine version and the backend image digest declared
  by the Chemclaw3 backend manifest. That is D-2026-10-21-every-compute-server-serves-a-calculation-key
  in this record.
- CLAUDE.md's "No DFT tier" paragraph is reworded on acceptance.

**Revisit when:** a parser needs to execute engine code to read its output (a binary checkpoint
format with no documented layout). That would bring an engine's dependency closure back, and the
answer is a different file format from the backend, not a binary in the pod.

## What keeps it true

Nothing specific to `qm` yet. The server's own egress and layout tests arrive with it. The fleet
guards it will be held by already exist:
`tests/test_fleet_egress.py::test_the_lint_ban_and_the_static_scan_name_the_same_modules` and
`tests/test_deploy_shape.py::test_the_egress_policy_denies_and_selects_the_workload`.

[`Chemclaw3`]: https://github.com/8fqycwdt8v-oss/Chemclaw3
