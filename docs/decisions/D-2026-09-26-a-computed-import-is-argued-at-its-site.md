# D-2026-09-26-a-computed-import-is-argued-at-its-site — A computed import is argued at its site

**Status:** accepted · **Date:** 2026-09-26 · **Supersedes:** §4.2 of
`D-2026-09-14-what-this-fleet-enforces-bounds-measures-and-accepts` ("A dynamic import of a computed
name is outside the static scan, and always will be"). The rest of that record stands.

## The choice

`mcp_server_kit.no_egress` resolves `importlib.import_module("gr" + "pc")` to `grpc`, and until
this record it passed `importlib.import_module(name)` in silence, because no static reader can
evaluate `name`. `docs/BACKLOG.md` §1 queued the question: is the runtime guard plus
`make offline-run` the whole answer, or does a server that loads modules by name owe a declaration
that *is* statically checkable? Two options:

- **Keep passing it.** The argument was that `servers/rxnpredict` loads its optional predictor
  plug-ins exactly that way, so flagging the shape "would fail correct code and teach the next
  reader to reach for `exempt`".
- **Report it, and require a justification naming the function it sits in.** Taken.

The argument for the first conflated two mechanisms. `exempt` skips a whole *file* — every import,
every host literal in it — and is reserved for a file whose network import is the disabling one.
What a computed import needs is narrower: one sentence, at one scope, saying which names it may load
and why none of them is a network client. That is not an exemption from the scan; it is the scan
asking a question the code cannot answer by itself and a person can.

## What changed

- `no_egress.computed_imports(source)` returns every `__import__(...)` / `import_module(...)` call
  whose module name `_constant_string` cannot fold — positional or `name=` keyword — as
  `(scope, line)`, where `scope` is the enclosing function or class (`"<module>"` at top level).
  A scope rather than a line, because a line number moves on every edit above it while "this
  function loads a plug-in by name" stays true until the function changes — which is when the
  argument should be read again.
- `assert_no_egress_sources(..., justified_imports={(file, scope): reason})` refuses every computed
  import without an entry, **and** every entry whose scope no longer holds one, **and** every
  entry whose reason is blank. Both directions, for the reason every allowlist in this repository
  is held in both: a justification that outlived its site reads as a live argument.
- Measured over every first-party `src/` tree at `7e3454d`: exactly two computed imports exist.
  `servers/pyexec/.../engine/runner.py::_guarded_import` is already `exempt` (it runs in the
  sandbox child and *is* the import guard), so it needs nothing new. The other is
  `servers/rxnpredict/.../engine/predictors/__init__.py::discover_predictors`, which is now
  justified in that server's `test_no_egress.py`.

## The manifest the row asked about

The backlog row suggested a plug-in loader could owe "a manifest of the module names it may load,
which *is* statically checkable". `rxnpredict` already had one — `_FORWARD_MODULES` and
`_CONDITIONS_MODULES`, literal dicts that exist for its degradation labels — so the justification
rests on them rather than on a new file, and two tests make the justification a checked claim
rather than a sentence:

- the loader iterates those two maps and nothing else, and `import_module` receives the loop's
  module name and nothing else (read as a tree, so a second source of names — an environment
  variable, a settings list, an entry point — fails the test instead of inheriting the argument);
- every name in the maps is under `chemclaw_mcp_rxnpredict.engine.predictors.`, under no forbidden
  root, and a file this package ships.

## What this does not change

An **address** assembled at runtime — an f-string, a `%` format, a `"".join` — is still outside the
static scan, and the module docstring says so. There is no call shape to demand an argument at: an
address is any string, and the place it becomes a connection is inside a library. That half stays
with the runtime guard and `make offline-run`, as §4.2 said of both.

## What keeps it true

- `packages/mcp_server_kit/tests/test_no_egress.py::test_a_dynamic_import_of_a_computed_name_must_be_justified_at_its_site`
  — a computed import (positional and keyword, in a function and a method) is an offence until
  justified by scope; a literal one is not reported.
- `packages/mcp_server_kit/tests/test_no_egress.py::test_a_justification_that_outlived_its_computed_import_is_refused`
  — a stale entry and a blank reason both fail.
- `servers/rxnpredict/tests/test_no_egress.py::test_no_module_can_reach_the_network` — the one
  justified site in the fleet.
- `servers/rxnpredict/tests/test_no_egress.py::test_the_plug_in_loader_imports_only_its_own_map`
  and `servers/rxnpredict/tests/test_no_egress.py::test_every_plug_in_the_loader_may_import_is_a_module_this_package_ships`
  — the two halves that make that justification a checked claim.
- `tests/test_decision_log.py::test_every_retired_citation_names_a_live_replacement` — §4.2's
  citation of the reversed test resolves to its replacement and to this record.
