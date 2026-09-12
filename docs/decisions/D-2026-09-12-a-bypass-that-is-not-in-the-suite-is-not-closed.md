# D-2026-09-12-a-bypass-that-is-not-in-the-suite-is-not-closed — A bypass that is not in the suite is not closed

**Status:** accepted · **Date:** 2026-09-12 · **Commit:** the follow-up to `c1772fb` (PR #54) in the
same wave. **No hash is written here for this pass**, deliberately: the work below is merged by
squash, so the hash this session could name is a branch commit that `main` will not contain — which
is the defect §5 is about, and writing one would be the third record in this repository to do it.

## Context

`c1772fb` replaced four ratchets that were measuring less than they claimed. Three fresh-context
reviewers then drove the replacements and walked past several of them: five controls had a bypass,
the two registers could be walked past in four ways between them, and a number of sentences written
in that commit were false or overstated — each is corrected below or at the line that carried it.
None of the five bypasses was exotic. Each is the spelling a developer would reach for
on an ordinary afternoon, and one of them — `ARG` → `ENV` indirection — **already ships in this
tree** for a legitimate reason.

The generalisation is the title. `c1772fb`'s own record says a bite-test must drive the shapes the
tree contains; what it did not say is that **a bypass somebody demonstrated is itself such a shape**.
A fix with no test case is a fix that holds until the next person writes the natural thing again, and
a reviewer's transcript is not a place a fix can live.

Every figure below is a measurement of `c1772fb` (the state the reviewers drove) or of this pass.
Nothing here is a claim about `HEAD`.

## Decision

### 1 · The egress ratchet flags a value it cannot *prove* arms the guard

`arm_from_env` arms unless `MCP_EGRESS_GUARD` holds one of four words, and the ratchet flagged those
four words. So it read every value it could not resolve as clean:

```
_egress_offences('servers/x/Containerfile', 'FROM x\nARG GUARD=off\nENV MCP_EGRESS_GUARD=${GUARD}\n')
  → []          # at c1772fb; a reviewer's `docker build` of it ships MCP_EGRESS_GUARD=off
```

The rule is inverted: an offence unless the value provably arms. Any `$` means the file does not hold
what the image will carry, and a `valueFrom:` reference does not either. The disable-set is no longer
transcribed into the test — `egress.GUARD_DISABLED_VALUES` is a module constant the runtime reads and
the ratchet imports, because a copy of a set is a second declaration and this repository keeps
deleting those.

**The one case that must stay clean is the empty value**, and it was reasoned about rather than run
until now: `os.environ.get("MCP_EGRESS_GUARD", "on")` returns `""` for a bare `ENV
MCP_EGRESS_GUARD=`, and `""` is in no disable set, so the guard arms. A ratchet that flagged it would
refuse the posture it exists to protect. `test_every_value_that_disarms_the_guard_is_in_the_set_the_ratchet_reads`
drives the whole table — every member, three case variants, and five values that must arm — through
the real `arm_from_env` rather than asserting it in a docstring.

### 2 · Three more shapes hid a value from *both* ratchets

All three returned `[]` at `c1772fb` and are offences now, each with its own arm in a bite-test:

- **A leading space.** Docker accepts `   ENV …`; the match was anchored at column 0 and
  `_instructions` never stripped; a reviewer confirmed the variable reaches the built image.
- **`valueFrom:`.** `_env_pairs` required a `value` key, so an entry pulling `MCP_EGRESS_ALLOW` out
  of a ConfigMap named the variable and reported nothing. It is now reported with `None` — the file
  sets the variable and does not hold the value — which both ratchets read as unprovable. `envFrom`
  was already refused for exactly this reason; this is the same file hiding the same thing one key
  deeper.
- **An assignment in `command:`** (or in a Containerfile `CMD`/`ENTRYPOINT`), which sets a variable
  for the server process with no `env:` block anywhere.

### 3 · A lowercase environment name moves a bound, and a bound now carries how it is matched

```
chemclaw_calc_max_concurrent_requests=99  →  CalcSettings().calc_max_concurrent_requests == 99
_bound_offences('servers/calc/Containerfile', 'ENV chemclaw_calc_max_concurrent_requests=99\n', …)
  → []          # at c1772fb, while the UPPERCASE spelling was caught
```

`pydantic-settings` leaves `case_sensitive=False`; the derivation uppercases a field's name and
matching was `name in bounds`. That is the admission ceiling of the server whose calls run from
minutes to hours.

Matching could not simply become case-insensitive, because the two mechanisms differ and measurement
says so: `mcp_max_smiles_chars=7` leaves `limits.MAX_SMILES_CHARS` at 4000, since `os.environ` is
case-sensitive. A uniform rule is wrong either way round — it misses the ceiling, or it flags an
`ENV` that changes nothing and teaches the next reader to add an exemption. So `numeric_env_bounds`
returns a `Bound` carrying `case_sensitive`, set by the mechanism that found it, and both directions
are asserted.

### 4 · The `ctypes` pin compares root packages, because a submodule is the same module

`c1772fb`'s message called the `ctypes` decision "pinned at both ends". The pin matched
`alias.name == "ctypes"` exactly, so `import ctypes.util`, `from ctypes.util import find_library` and
`importlib.import_module("ctypes")` all walked past it — and each of them binds a name with
`CDLL` behind it. The comparison is on the root package now, and the dynamic form goes through the
scanner's own `_dynamic_import_target` rather than a second copy of it. That helper also folds a
literal chain, so `import_module("gr" + "pc")` is the module it spells; the folding was already in
the file for `host_literals`.

### 5 · A commit citation must be reachable from `HEAD`, not merely exist

`D-2026-09-12-a-ratchet-measures-what-it-parses` cited `68083a4`, `39ba4a7`, `362e764` and `1161473`,
and told the reader to run `git show 39ba4a7:tests/test_fleet.py`. PR #54 was **squash**-merged, so
none of the four is an ancestor of `main`. Measured 2026-09-12 on a full (unshallowed) clone: all
four objects still resolve *here*, because this is the branch they were written on, and
`git merge-base --is-ancestor` reports none of them reachable from `HEAD`. That is the trap — the
citation works for its author and for nobody else, and `git cat-file -e` would have agreed with the
author forever.

**That record's citations are corrected rather than left standing**, which is the one edit a merged
record may take: every hash in it is now `c1772fb`, the merge commit that contains all four, and its
header says so and points here. The reproduction stays runnable — `git show
c1772fb:tests/test_fleet.py`, executed in place, returns `total 42 | calc 28`, which is the figure
that record reports for `39ba4a7`. A decision is not rewritten by making its evidence openable.
`docs/BACKLOG.md` cited `362e764` in the same state and is corrected the same way.

### 6 · The two registers are checked per row, per register, and by the definition they publish

Three holes, all of the same kind — a check that agrees with itself:

- **The anchor check counted globally.** `assert checked` passed on *some* row's hits, so a row whose
  every backticked token is shorthand (`mcp_server_kit/egress.py` is the shape, and `CLAUDE.md`
  writes it thirteen times) contributed nothing and was reported by nothing. Resolution is tracked
  per row.
- **`_ROW` required a bolded title** while the register's header publishes `grep -c '^- [ ]'` as the
  authority on what a row is. An unbolded row was a row to every reader and invisible to every check
  here. The parse takes any checkbox item, and a test asserts the two definitions agree.
- **A record filed under a non-`D-` name was invisible to `test_decision_log.py` entirely** — not a
  duplicate, not a dangling id, simply absent from a `D-*` glob. Everything beside the records must
  now be a record.

A fourth was two implementations of "a rooted path", written in the same commit and disagreeing:
`tests/test_fleet.py` filtered to `is_dir()` and the backlog's version did not, while its docstring
claimed "the same trick". They are one definition now — the backlog's, because a root-level file is
a legitimate anchor and `MODULES.md` is this repository's only port registry. **What that does not
buy is measured and written at the test**: renaming `MODULES.md` to a file that does not exist in a
`CLAUDE.md` citation leaves the check green either way, because "is this token a path" is decided by
whether its first segment exists — a single-segment citation stops being read as a path at the same
moment it stops resolving. The check covers a path *under* an entry that exists, which is every
citation naming a test, a module or a manifest.

The count-in-prose guard was the fifth: a number in a **code span** walked past it (a backtick is
how a careful author writes one), as did the singular, `a dozen`, `Rows open: 8`, a table cell and
`8 tasks`. It is now two patterns over backtick-stripped text, and it runs over
`docs/decisions/README.md` as well — which `CLAUDE.md` always claimed it bound and which nothing read.
One sentence in that ledger was rewritten to say "a second record on the same day" rather than "two
records on one day", because the guard cannot tell a design note from a stale count and the ledger is
the file where a stale count would actually hurt.

### 7 · What a vacuous test costs, and where it is recorded

`c1772fb`'s message says one new test came back vacuous and records it nowhere.
`test_two_records_on_one_day_are_distinct_ids` asserted `first.stem != second.stem` over two string
literals written in the test. Measured: filing a record under a date-only name left it green while
`test_every_filename_matches_its_heading` went red — so no state of this repository could fail it. It
now builds two same-day records and reads them back through the record machinery, so a glob that
stops matching or an id pattern that loses its slug fails here. The vacuity is recorded **at the
test**, not only in a commit message that no future reader will grep.

## Consequences

- **A demonstrated bypass is a test case.** Every one of the five above is in the suite as the exact
  input that walked past the old code, with its measurement beside it. A fix whose bypass is not in
  the suite is a fix with a half-life.
- **"Provably safe", not "not obviously unsafe."** Where a ratchet reads a file that may not hold the
  value (a build `ARG`, a ConfigMap reference), the unresolvable case belongs with the offences. The
  old rule's shape — enumerate the bad values — is safe only while the file is the whole story.
- **A number in a document is owed a date and an owner.** `docs/BACKLOG.md`'s figure about
  `Chemclaw3` is now dated and attributed, because a reviewer reading the same history against a
  *shallow* clone measured 1,534 lines where a full clone measures 4,737 and concluded the claim was
  false. Rule 4 of that register is the general form, and it applies to figures as well as to rows.
- **Two map claims are checked rather than asserted**: `scripts/README.md` against the directory
  (it listed one of the two scripts there for a month), and a `run-*` target per server on the port
  its own manifest publishes (`CLAUDE.md` published "one per server" over five targets for seven).

## What keeps it true

- `tests/test_fleet.py::test_no_shape_that_hides_a_value_from_this_ratchet_reads_as_clean` — the four
  egress bypasses, including the two arms that must stay clean.
- `tests/test_fleet.py::test_no_spelling_that_moved_a_bound_past_this_ratchet_reads_as_clean` — the
  lowercase spelling in both directions, and the same two hiding places over the bound set.
- `packages/mcp_server_kit/tests/test_egress.py::test_every_value_that_disarms_the_guard_is_in_the_set_the_ratchet_reads`
  — the disable-set as `arm_from_env` actually reads it, which is what the ratchet imports.
- `tests/test_fleet.py::test_the_derivation_reads_the_two_spellings_it_used_to_miss` — `os.getenv`
  and `Annotated[int, …]`, plus the boundary that stays open.
- `packages/mcp_server_kit/tests/test_no_egress.py::test_ctypes_is_outside_both_in_repo_layers_and_has_exactly_one_caller`
  — the root-package comparison and the three spellings that walked past it.
- `tests/test_decision_log.py::test_every_commit_the_registers_cite_is_reachable_from_head` —
  ancestry rather than existence, with a reported skip on a shallow clone.
- `tests/test_decision_log.py::test_a_record_filed_under_another_name_is_not_invisible` and
  `test_two_records_on_one_day_are_distinct_ids` — the unfiled record, and the test that used to
  assert nothing.
- `tests/test_backlog_register.py::test_every_anchor_a_row_names_exists`,
  `test_the_row_parse_agrees_with_the_headers_own_command`,
  `test_the_count_guard_catches_the_shapes_that_walked_past_it` and
  `test_the_decision_ledger_states_no_count_of_its_own_records` — the register checks, per row and
  per register.
- `tests/test_fleet.py::test_claude_md_and_the_guard_name_the_same_channels_as_outside_it` — the four
  channels, now against `FORBIDDEN_MODULES` rather than against a phrase only.
- `tests/test_fleet.py::test_every_path_claude_md_cites_under_a_real_directory_resolves` — one
  definition of a rooted path, with the limit of the heuristic measured in its docstring.
- `tests/test_fleet.py::test_the_scripts_map_lists_everything_beside_it` and
  `test_every_server_has_a_run_target_on_the_port_its_manifest_publishes` — the two map claims.
