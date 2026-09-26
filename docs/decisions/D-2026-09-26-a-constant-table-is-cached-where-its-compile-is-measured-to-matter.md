# D-2026-09-26-a-constant-table-is-cached-where-its-compile-is-measured-to-matter — A constant table is cached where its compile is measured to matter, and not on the strength of the pattern looking familiar

**Status:** accepted · **Date:** 2026-09-26 · **Builds on:**
`D-2026-09-18-an-output-cap-is-not-a-bound-on-the-work`, which cached `servers/chem`'s
`_ACIDIC`/`_BASIC`, measured it at 1.4x on a real molecule and nothing on a large one, and whose
docstring then called itself the last such fix.

## What was measured

Grepping `MolFromSmarts`/`ReactionFromSmarts` across `servers/*/src` after that record found five
more constant tables named by a backlog row. One of the five was not compiled per call at all:
`servers/rxnlabel`'s `engine/species.py` builds `_COMPILED` at import, so the row was stale on it.
The other four were timed in the `cc3-gate` Linux image (RDKit 2026.03.5, Python 3.11.14), best of
repeated runs, compiling the table against the whole public call. The host was under heavy unrelated
load (load average 30–500) throughout, so absolute times moved 2–3x between runs; the shares moved
far less, and the in-process A/B at the end compares like with like.

| table (patterns) | public call | compile per call | share of the call |
| --- | --- | --- | --- |
| `chem` `species.py::_TRANSFORMS` (11 reaction SMARTS) | `enumerate_degradant_candidates` | 0.39–2.1 ms | 32–50% tyrosine, 7–12% imatinib |
| `chem` `sites.py::_KINDS` (21) | `describe_atom_sites` | 0.22–1.19 ms | 9–11% tyrosine, 1.4–1.7% imatinib |
| `chem` `torsions.py::_KINDS` (6) | `enumerate_torsion_candidates` | 0.16–0.37 ms | 5.5–7.6% tyrosine, 3.6–4.8% imatinib |
| `rxnlabel` `agents.py` `_LIGAND_SMARTS` + `_BASE_SMARTS` (7 + 13) | `is_ligand` + `is_base` | 0.45–1.3 ms | 73–95% |

Cached, then timed in one process with the cache cleared per call against warm:

| call | per-call compile | cached | |
| --- | --- | --- | --- |
| `enumerate_degradant_candidates`, tyrosine | 1,588 µs | 1,262 µs | 1.26x |
| `enumerate_degradant_candidates`, imatinib | 4,578 µs | 3,970 µs | 1.15x |
| `is_ligand` + `is_base`, ethanol (walks both tables) | 611 µs | 140 µs | 4.4x |
| `is_ligand` + `is_base`, P(tBu)3 | 564 µs | 217 µs | 2.6x |

## What was decided

- **`agents.py` is cached** (`_compiled`, on the pattern string). Parsing was most of the call, and
  those two calls run per species across a whole labelling batch. None of its twenty patterns is a
  recursive SMARTS.
- **`_TRANSFORMS` is cached** (`_compiled_transforms`), and each reaction is `Initialize()`d inside
  the cache: `RunReactants` otherwise initialises lazily, which is a write to a shared object from
  whichever worker thread gets there first. Three of the eleven reactant templates are recursive,
  so it rests on the same `RDK_BUILD_THREADSAFE_SSS` build flag `_compiled` in the same module
  does, and its test drives the shared reactions from eight threads rather than assume it.
- **`sites.py` and `torsions.py` are not cached**, and each function's docstring now says so with
  its numbers. Both spend under about a tenth of a small call compiling, less on a large one, inside
  the run-to-run noise of the call itself; and both tables carry two recursive patterns, so a
  shared cache would add a thread-safety dependency to buy a saving that size. That is the trade
  the earlier record's own measurement warned about.

## What keeps it true

- `servers/chem/tests/test_species.py::TestTheTransformTableIsCompiledOnce::test_repeated_calls_build_the_table_once`
- `servers/chem/tests/test_species.py::TestTheTransformTableIsCompiledOnce::test_every_transform_compiles`
- `servers/chem/tests/test_species.py::TestTheTransformTableIsCompiledOnce::test_the_shared_reactions_answer_correctly_under_concurrency`
- `servers/rxnlabel/tests/test_agent_tables.py::test_each_role_smarts_is_compiled_once_per_process`

The two tables left uncached have nothing holding them uncached, deliberately: caching one later is
a measurement and a commit, not a regression.
