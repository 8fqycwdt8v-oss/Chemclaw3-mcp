# D-2026-09-12-a-ratchet-measures-what-it-parses — A ratchet measures what it parses, not what it is named after

**Status:** accepted · **Date:** 2026-09-12 · **Commit:** `c1772fb` (PR #54)

> **Citations corrected 2026-09-12** by
> [`D-2026-09-12-a-bypass-that-is-not-in-the-suite-is-not-closed`](D-2026-09-12-a-bypass-that-is-not-in-the-suite-is-not-closed.md):
> the four branch commits this record named did not survive the squash merge, so every hash below is
> now the merge commit that contains that work, and the reproduction in §2 runs against it. Nothing
> else is changed — a decision is not rewritten by making its evidence openable.

## Context

This fleet's posture is held by ratchets: a test that reads the shipped tree and fails when
something moves. `CLAUDE.md` names four independent layers for no-egress "because a rule that lives
in one place rots", and it tells a slow tool it owes the fleet a bound on its input and a ceiling on
how many of it may run at once. Four passes over those ratchets found the same defect in each: the
check was named after the property, and what it *parsed* was narrower than the property. A ratchet
in that state cannot go red. It can only be believed.

Every figure below is a measurement of the commit named beside it. Nothing here is a claim about
`HEAD`; the live figures are held by the tests in **What keeps it true**.

## Decision

### 1 · Parse the file's own grammar, not its lines (`c1772fb`)

`_egress_offences` matched `^ENV\s+(MCP_EGRESS_[A-Z_]+)=(.*)$` against a Containerfile's raw text.
Docker's `ENV` takes a backslash continuation, and two servers use it: measured across the seven
shipped Containerfiles, that pattern saw `MCP_EGRESS_GUARD` in five, and the two it missed —
`servers/rxnlabel` and `servers/rxnpredict` — are the two that set it under a continuation. The
ratchet would have been equally silent had the value been `off` with `MCP_EGRESS_ALLOW` beside it.

`test_the_allowlist_check_bites` is why that survived a bite-test. It drove one shape,
`ENV MCP_EGRESS_GUARD=off` on its own line, which is precisely the shape those two files do not use
— **a bite-test that certifies the arm the tree does not exercise proves the ratchet works on
nobody's code.** The parse is now Docker's: comments dropped first, continuations joined, then
`ENV k=v k2=v2` split with `shlex`, plus the legacy `ENV name value` form. Both arms are driven, and
`_env_settings` is the one reader every ratchet over these files uses, so two of them cannot
disagree about what a file sets.

### 2 · Derive the bound set by AST, because a grep cannot see a settings field (`c1772fb`)

**Nothing ratcheted a resource bound at all.** The egress check read every shipped `deploy/*.yaml`
and Containerfile and discarded every environment pair whose name was not `MCP_EGRESS_*`, so an
`env:` entry could double an admission ceiling, widen the SMILES bound that stops a SIGSEGV, or
retune the pKa calibration constants that key Chemclaw3's ledger, with nothing going red.

The survey behind this work listed twelve variables to protect. Derived from the code that reads
them, the inventory measured **42** (at `c1772fb`). That survey also
exempted `servers/calc` — the heaviest server in the fleet, whose calls take minutes — on the
grounds that its bounds "are constants, not env-readable". Measured at `c1772fb`:

```
CHEMCLAW_CALC_MAX_CONCURRENT_REQUESTS=99 CHEMCLAW_XTB_MAX_ATOMS=99999
  → calc_max_concurrent_requests 4 → 99,  xtb_max_atoms 500 → 99999
```

**Why the survey was wrong, in a form the next reader can reuse: an `os.environ`/`getenv` grep
cannot see a `pydantic-settings` field.** `CalcSettings` declares `env_prefix="CHEMCLAW_"`, so its
numbers look like constants on the page and *are* environment variables — the variable's name
appears nowhere in the source. A grep-derived bound set therefore reports clean exactly where the
exposure is largest. The set is derived by AST over both mechanisms instead, and it is derived
rather than listed because a hand-written list of names is the drift hazard this repository keeps
finding — `CLAUDE.md`'s deleted port table is the worked example, a second declaration nothing
checked that published two taken ports as free.

**A worked example of this record's own rule about numbers, and the first draft of this paragraph
was the second example.** `c1772fb`'s commit message says calc owns 25 of the inventory; the same
derivation returns 28. This paragraph originally reported that as a drift — "returns 28, with no line
of `engine/config.py` rewritten between them" — which is a claim that the ratchet's basis moves under
it, and would be the more serious finding of the two. It is false. Running the derivation **as that
commit itself wrote it** (`git show c1772fb:tests/test_fleet.py`, executed in place so its
`parents[1]` root still resolves) returns `total 42 | calc 28`, and five consecutive runs at `HEAD`
return 42 every time. The number was wrong when it was written, not when it was re-read; nothing
drifted and nothing is unstable. The reason it cost nothing is that
`test_the_bound_scan_sees_both_configuration_mechanisms` holds a floor rather than the digit — which
is the rule this section is about, now demonstrated twice: once by the miscount, and once by a
paragraph that reached for the more alarming explanation before measuring which one was true.

### 3 · "Sets it at all", not "widens past the default"

The widening form (`value > default`) is the stronger rule only where the default is discoverable
and the direction is known, and neither holds here. `MCP_THREAD_POOL_SIZE` and
`MCP_SESSION_IDLE_TIMEOUT_SECONDS` are read with an empty-string default and get their real one from
a constant or a cgroup at runtime; for calc's fitted pKa constants "wider" means nothing at all.

The counter-example is the one row the check found on its first run over the real tree:
`servers/calc/Containerfile` sets `CHEMCLAW_CREST_THREADS=4` against a default of `0`, which means
"let CREST's OpenMP size itself from `/proc/cpuinfo`" — the node's core count, which a container CPU
limit does not change. That is a **narrowing**, and a default comparison would have scored it `4 > 0`
and waved it through as a widening. It is recorded as a (file, variable) pair with its reason, so the
same variable set from another file is still an offence.

### 4 · `grpc` is on the static scan's list; `ctypes` is deliberately off it (`c1772fb`)

`arm()` patches nine callables and all nine refuse and count. A compiled extension is outside that
by construction, and `grpcio` is one — it is in `uv.lock`, pulled under `servers/rxnpredict`'s ML
extras by `tensorboard`. Measured against a listener on a non-loopback address with the guard armed:
a Python `socket.create_connection` raised `EgressForbidden` and booked a refusal on
`chemclaw_mcp_egress_refused_total`, while `grpc.insecure_channel` to the same address completed a
real TCP connection and booked none. That puts `grpc` in `_socket`'s position — the static scan is
the only in-repo layer that can see it arriving — so it is on the list, with a prefix match covering
`grpc.aio`.

`ctypes` is decided the other way and the reason is in the tree rather than in a preference.
`ctypes.CDLL("libc.so.6").connect(...)` walks past the guard for grpc's reason, but
`servers/pyexec/.../engine/sandbox.py` imports it to call `prctl(PR_SET_DUMPABLE, 0)` — one
constant, not a dependency. Banning the module means exempting that file, and `exempt` is reserved
for a file whose network import is the *disabling* one; measured by adding `ctypes` to the list,
pyexec's own `test_no_egress.py` fails on correct code, which is how an exemption granted for a
false positive teaches the next reader to reach for one. So the case is **stated** rather than
inferable from a clean scan, and pinned from both ends: the module stays off the list, and the set
of files importing it stays at the one whose use was argued.

### 5 · A document about a mechanism is checked against the mechanism (`c1772fb`)

`egress.py`'s docstring names four channels outside the runtime guard by construction. `CLAUDE.md`
§1 named three, and the omitted one is the one the guard *provably* cannot reach: the private C type
`_socket.socket`, because `arm()` rebinds the methods of the Python `socket.socket` subclass, never
the C type it inherits from. A reader of the shorter list takes the static scan's `_socket` entry for
belt-and-braces rather than for the only in-repo layer that sees it. Two paragraphs about one
mechanism, written months apart, and nothing compared them.

§4 cited `tests/test_deploy.py` as what asserts the NetworkPolicy in both directions. **That file has
never existed** — the assertion is each server's own `servers/*/tests/test_deploy.py`, and the root
file with the closest name, `tests/test_deploy_shape.py`, carries no egress assertion at all. A
citation to a file nobody can open is the deleted port table, one document over. Every path
`CLAUDE.md` cites under a real top-level directory is now resolved; that needs no allowlist, because
the document's deliberate shorthands (`app.py`, `connector.yaml`) begin with no directory that
exists here.

## Consequences

- A bite-test must drive **the shapes the tree actually contains**, not the shape the author had in
  mind. Where a ratchet parses a file format, the bite-test's arms are that format's grammar.
- A protected set is **derived from the code that reads it**. A list of names in a test is a second
  declaration, and this repository has already published one that was wrong.
- The four no-egress layers are stated with their gaps: `ctypes` and a child process are outside
  both in-repo layers, by decision and by construction, and only `make offline-run` covers them —
  which is not in `make check`. That is queued in [`../BACKLOG.md`](../BACKLOG.md) rather than
  implied by a clean scan.
- What is still unreachable by any of this: a scalar-typed settings field is covered and a container
  one is not; and a pod `env:` a cluster operator adds outside these files is seen by nothing here.
  Both are queued with their anchors.

## What keeps it true

- `tests/test_fleet.py::test_no_shipped_deployment_widens_the_egress_allowlist` and
  `test_the_allowlist_check_bites` — the guard, and its bite-test driving both grammars.
- `tests/test_fleet.py::test_no_shipped_deployment_moves_a_bound_the_code_reads_from_the_environment`
  and `test_the_bound_check_bites` — the resource-bound ratchet over the derived set.
- `tests/test_fleet.py::test_the_bound_scan_sees_both_configuration_mechanisms` — that the scan sees
  a `pydantic-settings` field, which is the half a grep loses, and that calc's share has not
  collapsed.
- `servers/calc/tests/test_admission.py::test_the_ceiling_is_an_environment_variable_and_not_a_constant`
  — the same fact as an effect: the settings object is built twice and the numbers differ.
- `packages/mcp_server_kit/tests/test_no_egress.py::test_a_grpc_channel_is_flagged_however_it_is_spelled`
  and `test_ctypes_is_outside_both_in_repo_layers_and_has_exactly_one_caller` — both halves of §4.
- `tests/test_fleet.py::test_claude_md_and_the_guard_name_the_same_channels_as_outside_it` and
  `test_every_path_claude_md_cites_under_a_real_directory_resolves` — §5, in both directions.
