# D-2026-09-18-an-output-cap-is-not-a-bound-on-the-work — An output cap is not a bound on the work

**Status:** accepted · **Date:** 2026-09-18 · **Commit:** `enumerate_microstates` gains an input
bound and the `_ACIDIC`/`_BASIC` SMARTS gain a `@cache`, in that order, because the backlog row that
asked for the second said the first had to come first and it was right for a larger reason than it
knew.

## What was asked for, and what measuring it found

`docs/BACKLOG.md` carried one row: `servers/chem`'s `engine/species.py::_sites` re-parses eleven
SMARTS constants on every call, the same defect `servers/safety` and `servers/rxnpredict` had
already fixed, measured at 28% of an `enumerate_microstates` call on tyrosine. The row refused to be
a commit on its own, on the grounds that "`enumerate_microstates` is `chem`'s heaviest tool and
nothing here bounds or measures its latency, so the honest order is a bound first and then the
saving."

Measuring the bound is what found the actual defect, and it is not a latency defect. **Every bound
in this server stops a cost before it is paid except one.** `MCP_MAX_MOLECULE_ATOMS` refuses a
megamolecule at the parse. `StereoEnumerationOptions(maxIsomers=MAX_STEREOISOMERS + 1)` stops the
stereo enumerator one past its cap instead of materialising 2^n — a fix this file's own comment
records as taking a refusal from 28 s to 0.06 s. `enumerate_microstates` had the *shape* that fix
was written for and none of the fix: it shifted, sanitised and canonicalised **every** ionisable
site and only then compared the species count with `MAX_MICROSTATES`.

Two molecules inside every bound this server had, measured on this container:

| molecule | heavy atoms | sites | before | outcome |
| --- | --- | --- | --- | --- |
| `"N" + "CCN" * 659` | 1,978 | 660 | **48,077 ms** | `ValueError` — nothing returned |
| `"C1" + "CNC" * 659 + "CN1"` | 1,980 | 660 | **15,647 ms** | **answered**, with two species |

The first is the stereo defect exactly: the cap made the refusal certain, and nothing made it cheap.
48 s is past `connector.yaml`'s own `request_timeout: 30`, so the caller has already gone, and the
worker thread is not cancellable — `asyncio.to_thread` returns to a caller who is no longer there
while the CPU keeps burning, which is the control `CLAUDE.md` already says a per-call wall clock in
the transport cannot be.

**The second row is why the fix could not be the stereo fix.** Those 660 amines are all equivalent,
so every microstate collapses to the same canonical string and the answer is two species, inside
`MAX_MICROSTATES` at any value it could take. An output cap — even one consulted incrementally, one
species at a time — never fires here. The tool spends 15 s and returns two structures, and no bound
on what it *returns* can see that. Only a bound on what it is *given* can.

## The bound, and why 32

`MAX_IONISABLE_SITES`, checked against `len(acidic) + len(basic)` before any proton is moved. The
two `_sites` passes it reads are the two the function already made, so the check costs nothing —
perceiving 660 sites on a 1,978-atom molecule is 7.4 ms; walking them is the 48 s.

Derived twice, on purpose, and the two agree.

**By cost.** At the largest molecule the parse bounds admit, decoupling site count from atom count
(a ~1,900-atom chain carrying exactly *k* amines):

| sites | heavy atoms | wall |
| --- | --- | --- |
| 2 | 1,900 | 158 ms |
| 8 | 1,900 | 225 ms |
| 16 | 1,900 | 385 ms |
| 32 | 1,900 | **640 ms** |
| 48 | 1,900 | 1,035 ms |
| 64 | 1,900 | 1,407 ms |
| 128 | 1,900 | 3,047 ms |
| 256 | 1,900 | 10,253 ms |

So ~158 ms of parse and canonicalisation plus ~15 ms per site, and the yardstick is this server's
own other tools at the same molecule size: `describe_topology` **615 ms**, `enumerate_tautomer_set`
**400 ms**, `enumerate_stereoisomer_set` **367 ms**, `enumerate_degradant_candidates` **106 ms**. 32
sites puts the worst legal `enumerate_microstates` call *inside* that band; 48 puts it outside it.

**By contract.** A microstate here is one site toggled, so the answer is at most `1 + sites`. A
molecule with more ionisable sites than `MAX_MICROSTATES` can only produce an answer inside that cap
by degeneracy — which is the 15 s row above, the case worth refusing rather than the case worth
protecting.

It is `env_bound`, not a literal: a bound nobody can loosen for a real polyelectrolyte is a bound
somebody edits the image around, and `CHEMCLAW_CHEM_MAX_IONISABLE_SITES=0` is a startup failure
rather than a pod that serves nothing.

`describe_molecule` is deliberately **outside** the bound. It is the free, total tool a caller
consults to decide whether an enumeration is worth asking for, so "this molecule has 660 ionisable
sites" is precisely the answer that should reach them — and it is what the refusal tells them to go
and ask. It still answers that 660-site macrocycle in 268 ms.

## No second admission ceiling, and that is a measurement rather than an omission

`CLAUDE.md` says a slow tool owes two things: a bound on its input, and a ceiling on how many may
run at once. `servers/chem` already has the second for `render_structure`, and
`D-2026-09-12-one-tool-call-is-not-one-thread` establishes that whether a tool needs one is not the
manifest's `read_only`/`state_changing` split — `enumerate_protonation_states` is `read_only` and
correctly so. So the question had to be asked, and it was driven rather than argued.

`enumerate_microstates` does hold the GIL. Four concurrent calls of a 1.13 s molecule on a four-core
box: `wall` 5.15 s, `cpu` 4.76 s, `cpu_util` **0.93x**, throughput falling from 0.87/s at one thread
to 0.75/s at eight — the same signature `engine/admission.py` records for `Compute2DCoords`.

Holding it does **not** starve the loop the readiness probe is answered on, which is the thing
`DEFAULT_MAX_CONCURRENT_RENDERS` is derived to protect. Bursts of the worst molecule the new bound
admits, driven through the real tool coroutine with a 0.1 s event-loop tick beside them:

| concurrent calls | wall | tick lateness p50 | tick lateness max |
| --- | --- | --- | --- |
| 1 | 0.67 s | 12.7 ms | 24.9 ms |
| 2 | 1.27 s | 14.9 ms | 103.9 ms |
| 4 | 2.96 s | 47.6 ms | 167.5 ms |
| 5 | 3.67 s | 53.4 ms | 580.4 ms |
| 8 | 5.23 s | 70.1 ms | 384.2 ms |

and one *unbounded* 14 s call left the tick **46.0 ms** late at worst. `readinessProbe.timeoutSeconds`
is 3. Nothing here comes near it, because the GIL is preemptive at a finer grain than a call, so the
"N x worst-call" arithmetic that derives the render ceiling is conservative by an order of magnitude
for this shape.

A ceiling would also be the wrong instrument for the harm that was actually measured. The harm is
**one** call burning 48 s past its caller's timeout; a ceiling bounds how many run, never how long
one runs. And the pod's concurrent CPU is already bounded by something narrower than any ceiling
this server would write — `POD_THREAD_POOL_WIDTH` is 5 against a render ceiling of 8, which
`engine/admission.py` already records as the reason that ceiling never binds. A second one at 5 or
above would be a knob that renders nothing; below 5 it would refuse callers this pod answers in
0.6 s against a 30 s budget.

What that leaves genuinely open — whether the five enumerators should share one ceiling, given that
`describe_topology` at 615 ms is ungated while a 4.6 ms worst-legal depiction is gated at 8 — is a
`docs/BACKLOG.md` row carrying these numbers, not a knob added here on the strength of a measurement
that says it would not bind.

## The saving that was asked for

`_compiled` is `@cache`d on the SMARTS string, following `servers/rxnpredict`'s
`engine/meta/classifier.py::_compiled` rather than pre-compiling at import — this server loads its
corpus lazily on purpose (`D-2026-09-18-a-corpus-that-cannot-be-read-is-a-probe-s-answer-not-an-import-error`)
and eleven module-scope parses would be a second thing that can fail in an import.

Measured against the exact code it replaced, five runs of 2,000 iterations on tyrosine:

| | before | after | |
| --- | --- | --- | --- |
| `_sites`, both tables | 209–227 µs | 35.5–35.9 µs | **5.8x – 6.4x** |
| whole `enumerate_microstates` | 853–890 µs | 602–620 µs | **1.39x – 1.43x** |
| `enumerate_microstates`, 1,891 atoms / 31 sites | 580 ms | 595 ms | 1.0x (noise) |

So ~30% of a small call, which is the 28% the row measured four days earlier on a machine this is
not. **The saving is a constant rather than a factor** — ~250 µs per call whatever the molecule — so
it is a third of a small call and nothing at all in a large one. The row's ordering was right and
its emphasis was the other way round: the bound is worth 356x on one shape and 474x on the other,
and the cache is worth 1.4x on the shape a chemist actually submits.

The same pass made `describe_molecule` perceive each table once rather than twice: it ran `_sites`
four times for three numbers, two of which are the other two added together.

**And `_compiled`'s docstring shipped, briefly, claiming to be "the third and last place the fleet
had it".** Grepping `MolFromSmarts`/`ReactionFromSmarts` across `servers/*/src` in the same session
found four more constant tables compiled per call, one of them fifty lines further down the same
file (`_TRANSFORMS`, eleven reaction SMARTS rebuilt on every `enumerate_degradant_candidates`).
None is measured, so none is fixed here and none is claimed either way; they are a `docs/BACKLOG.md`
row. A closing claim about a whole fleet, written from the one call site that was under the hand at
the time, is the shape `CLAUDE.md`'s deleted port table is about — and it was caught by running the
grep rather than by rereading the sentence.

## What keeps it true

- `servers/chem/tests/test_microstate_bound.py::test_the_refusal_is_reached_without_walking_what_it_refuses`
  and `::test_a_molecule_whose_sites_collapse_is_refused_by_the_same_bound` — the two rows of the
  first table, each refused inside a second where they cost 48 s and 15.6 s. The second is the one
  no output cap can hold, so it is asserted separately rather than folded into the first.
- `servers/chem/tests/test_microstate_bound.py::test_a_molecule_at_the_bound_is_not_refused_by_the_bound`
  — which of the two bounds fires at exactly `MAX_IONISABLE_SITES`, read off the message, so an
  off-by-one in either direction is visible.
- `servers/chem/tests/test_microstate_bound.py::test_the_worst_call_the_bound_admits_stays_inside_the_probe_budget`
  — the cost derivation, driven against `PROBE_TIMEOUT_SECONDS` rather than against the 640 ms
  measured here, so a slower runner does not red the gate while a loosened bound does.
- `servers/chem/tests/test_microstate_bound.py::test_ordinary_chemistry_is_untouched_by_the_bound`
  and `::test_the_free_tool_still_answers_for_a_molecule_the_enumeration_refuses` — the two halves
  of "it refuses nothing real": the molecules this tool exists for, written out, and the free tool
  that must keep answering for the molecule the enumeration turns away.
- `servers/chem/tests/test_microstate_bound.py::test_the_refusal_names_the_sites_rather_than_the_species_it_did_not_count`
  — the refusal quotes a number it measured, not one it would have had to enumerate to know.
- `servers/chem/tests/test_microstate_bound.py::test_the_bound_is_read_from_the_environment_and_is_not_a_constant`
  and `::test_a_bound_that_would_refuse_every_ionisable_molecule_stops_the_import` — the two halves
  `D-2026-09-12-a-bound-that-can-be-set-to-zero-has-to-say-what-zero-means` asks of every bound here.
- `servers/chem/tests/test_microstate_bound.py::test_the_cache_answers_exactly_what_a_fresh_compile_answers`
  and `::test_a_cached_pattern_gives_the_same_matches_on_its_second_use` — the cache proven
  *equivalent* over a corpus spanning both tables and their deliberate overlaps, against an
  independently written uncached implementation rather than a mock. A cache that is fast and wrong
  is the failure worth catching.
- `servers/chem/tests/test_microstate_bound.py::test_each_pattern_is_compiled_once_per_process`,
  `::test_every_pattern_in_both_tables_compiles` and `::test_the_topology_tool_perceives_each_table_once`
  — the mechanism, read off the real `functools` counters. The last is the only place
  `describe_molecule`'s duplicate passes were ever visible: with the cache warm they cost matching
  rather than parsing, so nothing else in this server would have gone red if they came back.
