# D-2026-09-15-the-eighth-server-arrived-with-the-shape-the-backlog-predicted — The eighth server arrived with the shape the backlog predicted

**Status:** accepted · **Date:** 2026-09-15 · **Commit:** the admission-ceiling derivation in
`tests/test_fleet.py`. Supersedes nothing; it closes the `docs/BACKLOG.md` row that predicted
this exact case, and applies `D-2026-09-12-one-tool-call-is-not-one-thread`'s finding — that the
manifest's `read_only`/`state_changing` split does not carry — to the question of which servers
owe a ceiling at all.

`docs/adding-a-server.md` asks a new server for "a ceiling on how many of it may run at once", and
nothing checked that it was given or refused. Five of seven servers shipped an `engine/admission.py`
— `calc`, `chem`, `pyexec`, `rxnlabel`, `rxnpredict` — each held by its own `test_admission` module.
The absence of a sixth was held by nobody. `docs/BACKLOG.md` recorded exactly that, and named the
case it would produce: *"an eighth server without one passes every test here."*

The eighth server then arrived. `servers/thermalsafety` was added on 2026-09-15 with no ceiling and
an argument for why it needs none — written in its `README.md`, which is precisely the shape this
repository already records as **a README is not a gate**. It passed the whole fleet suite. Nothing
in the run distinguished "this server has thought about its concurrency and decided it needs no
bound" from "nobody asked".

## Why it cannot be derived

The obvious derivation is the manifest's `read_only`/`state_changing` split, and
`D-2026-09-12-one-tool-call-is-not-one-thread` measured that it does not carry:
`render_structure` is `read_only`, correctly — it mutates nothing — and is the one tool in
`servers/chem` that needs a ceiling, because it generates 2D coordinates and draws under the GIL.
A derivation from the manifest would have exempted the one server the measurement says must not be
exempt.

Duration does not carry either, for the reason `CLAUDE.md` already gives about where a slow tool
belongs: `props` is milliseconds and needed an input bound; `calc` is minutes and needs a ceiling
counting *threads* rather than calls. Neither of those is a property of the manifest.

## What was built

The same shape as `BLIND_ANSWER_IS_ARGUED`, which this repository already runs for the other claim
no static reader can settle: **present, or argued in the test file, and checked in both
directions.**

- `test_every_server_either_bounds_its_concurrency_or_argues_why_it_need_not` — every directory
  under `servers/` either ships an `engine/admission.py` or holds an entry in
  `CEILING_IS_ARGUED_ABSENT`. A ninth server owes the same answer on the day it is added.
- `test_no_server_is_argued_out_of_a_ceiling_it_actually_has` — the reverse. A server that grows
  real work and adds a ceiling must lose its exemption in the same commit, or the next reader finds
  an argument for "this one is arithmetic" sitting beside a module bounding its concurrency and
  cannot tell which is true. Same rule `DEFERRED.md` states for a closed row.

Both arms were driven against a deliberate defect rather than assumed:

- A fixture ninth server under `servers/` with neither a ceiling nor an entry fails the first with
  `['probe9'] bound nothing about how many of their tools may run at once`.
- An entry naming `props`, which does not ship one, fails the second with `['props'] ship an
  `engine/admission.py` and are also argued not to need one`.

The second arm matters more than it looks. A one-directional check is satisfied forever by the
allowlist growing, and this repository has the record to prove it: the port table that listed five
servers when seven were built, and the `rxnpredict` cache branch a paragraph described after it was
deleted.

## The arguments are measurements, on one basis

Each of the three exemptions carries the figure behind it, not the adjective. "It is fast" is what
every server's author believes on the day they write it, and `props` is the counter-example already
in this tree — a dict lookup and a bisection, and 100 000 x `"dcm"` held the event loop for 14.83 s
with a `/healthz` probe stuck behind it.

Every figure is **engine CPU per call**: `time.process_time` around the engine function, warmed so
the lazy dataset load is not in the average.

- `props` — 0.7 µs for the table lookup, 1.8 µs for `vapour_pressure`, 10.3 µs for a Hansen sweep
  across the whole 44-row table, which is the largest single call `MAX_COMPARED_SOLVENTS` permits
  because that bound *is* the table's size.
- `safety` — 321 µs to screen a 37-heavy-atom drug structure, 339 µs for the genotoxic alert pass.
  RDKit holds the GIL, which is *why* `chem` needs a ceiling and this does not: `render_structure`
  generates 2D coordinates and draws, tens of milliseconds, two orders of magnitude above a
  substructure match against a fixed table.
- `thermalsafety` — 0.25 µs, 63.7 µs and 4.5 µs for its three representative tools, the middle one
  being a fixed 200-step bisection and the slowest of its seven.

**Writing them on one basis is the part that took two attempts**, and it is the same defect this
family records elsewhere as a narrowing argued across two bases. The first draft of the table gave
`props` as "11.7 ms of CPU" beside `thermalsafety`'s "63.7 µs" — and those measured different
things. The millisecond figures were whole MCP round trips, mostly the transport every server in
this fleet pays; re-measured on the engine alone, `props` is 1.8 µs, not 11.7 ms. The conclusion
did not change and the comparison was meaningless, which is the worse of the two failures: a reader
comparing 11.7 ms against 63.7 µs would conclude `props` is the one to watch, and it is the
cheapest of the three by two orders of magnitude.

The same thing had already happened once inside `thermalsafety` itself. Its prose first named
`semenov_critical_ambient` as its slowest tool at roughly 60 µs; timed, the slowest is `tmr_ad` at
63.7 µs, because it runs a fixed 200-step bisection, and `semenov` is 16.3 µs. Twice, on the same
table, the number that was recalled rather than run was wrong — which is the argument for measuring
stated as evidence rather than as a preference.

## What keeps it true

- `tests/test_fleet.py::test_every_server_either_bounds_its_concurrency_or_argues_why_it_need_not`
- `tests/test_fleet.py::test_no_server_is_argued_out_of_a_ceiling_it_actually_has`
