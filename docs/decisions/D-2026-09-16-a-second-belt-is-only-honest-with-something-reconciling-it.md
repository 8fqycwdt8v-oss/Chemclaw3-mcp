# D-2026-09-16-a-second-belt-is-only-honest-with-something-reconciling-it — A second belt is only honest with something reconciling it

**Status:** accepted · **Date:** 2026-09-16 · **Commit:** ruff `TID253` over
`no_egress.FORBIDDEN_MODULES`. Revisits
`D-2026-09-13-the-rule-that-would-have-caught-it-was-not-the-one-asked-for` on purpose and does not
supersede it: that record's refusal stands, and this takes the trade only because the thing it was
missing now exists.

## The refusal this starts from

`S101` is off fleet-wide, and `pyproject.toml` says why in as many words: turning
`test_no_serving_module_enforces_an_invariant_with_assert` into a `per-file-ignores` block "would be
a second declaration of one rule with nothing reconciling the two: the defect this repository
deleted a port table over".

A lint ban on the network modules is the same trade. It is taken here because the missing half is
supplied: `tests/test_fleet.py::test_the_lint_ban_and_the_static_scan_name_the_same_modules` reads
`pyproject.toml`'s `banned-module-level-imports` against `mcp_server_kit.no_egress.FORBIDDEN_MODULES`
**in both directions**, so a module added to either alone is red.

## Why two at all — what each sees that the other does not

`no_egress.network_imports` stays the control and is unchanged. It alone:

- folds `__import__("gr" + "pc")` to `grpc` through `_dynamic_import_target`;
- scans `host_literals` for an address in source;
- carries the per-server `exempt` mechanism `servers/pyexec/engine/runner.py` needs, and the test
  that server owes for using it.

Ruff does none of that. What ruff does that the scan does not is read the **whole tree** on every
`make lint`, where the scan runs per server from inside a test that has to be pointed at a
directory. A `packages/*/src` module no server's `test_no_egress.py` names is covered by the ban the
day it is written and by the scan never — and that is the gap this closes, not a duplication of the
one already covered.

`banned-module-level-imports` rather than `banned-api`, because a function-level import is how this
fleet writes an optional dependency (`mcp_server_kit.tracing`) and the scan takes the same line.

## The exemption list, which is the condition for keeping it

Measured when the ban was added: **43** module-level imports in this tree trip it. **41** are in a
`tests/` directory or in `scripts/` — which is exactly where `assert_no_egress_sources` does not
look, since it is pointed at a server's `src/<package>`. So those two entries draw the boundary the
control already draws rather than a wider one.

**Two files in `src/` trip it, and each carries its reason at the import** — the shape the record
above settled on for `BLE001`, so that a reader of the import finds the argument beside it:

- `mcp_server_kit/egress.py` — the guard *is* the rebinding of `socket`'s methods.
- `mcp_server_kit/testing.py` — the `[testing]`-extra helper that drives a running server, which no
  serving image imports.

The plan that proposed this ban predicted four, naming `no_egress.py` and `sessions.py` as well.
Measured, neither holds a module-level network import. The condition attached to the proposal was
that the exemption list be dropped — and the ban with it — if it grew past readable; two site-level
`noqa`s and two directory entries is what it is, and `test_the_lint_ban_names_its_exemptions_and_they_are_the_scan_s_own_boundary`
is what makes a third one a decision somebody has to argue rather than a line somebody adds.

## What keeps it true

- `tests/test_fleet.py::test_the_lint_ban_and_the_static_scan_name_the_same_modules` — the
  reconciliation, in both directions. Without this test the ban should not exist.
- `tests/test_fleet.py::test_the_lint_ban_names_its_exemptions_and_they_are_the_scan_s_own_boundary`
  — the exemption list, held to the two directories and the two files argued above.
- `packages/mcp_server_kit/tests/test_no_egress.py::test_a_dynamic_import_with_a_literal_name_is_flagged`
  and `test_the_private_c_socket_type_is_flagged` — the control
  itself, unchanged, which is what makes this a belt rather than a replacement.
