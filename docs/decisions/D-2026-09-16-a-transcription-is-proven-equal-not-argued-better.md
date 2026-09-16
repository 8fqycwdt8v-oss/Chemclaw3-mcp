# D-2026-09-16-a-transcription-is-proven-equal-not-argued-better — A transcription is proven equal, not argued better

**Status:** accepted · **Date:** 2026-09-16 · **Commit:** `servers/calc`'s ionisable-site
perception as a SMARTS table, and the `servers/rxnpredict` classifier's compile-once cache.
Supersedes nothing.

## What changed and what must not

`servers/calc/src/chemclaw_mcp_calc/engine/pka.py` perceived its acidic and basic sites with about
fifty lines of `GetBonds()` walking across four functions: `_acidic_protons`, `_conjugate_bases`'s
input, `_lone_pair_is_available` (amide/carbamate/urea/sulfonamide, nitrile, pyrrole-type) and
`_is_aryl_nitrogen`. Those rules are now three compiled SMARTS.

**The constraint that shaped this is that the perception may not change.** `pka.calc_version()` is
the primary key of Chemclaw3's calibration ledger — `predictions` is unique on
`(calc_type, calc_version, input_hash)` and `reconciled_for` matches it exactly, with **no version
pooling** — and the linear calibration was fitted over *this* enumeration. A site rule that
perceives one extra nitrogen picks a different most-stable protomer on some molecule, and every
residual recorded against that version becomes unreachable without anything failing.

So `dimorphite-dl` is **declined**. It perceives a broader, better-curated site set, which is
precisely the property that makes it unusable here: it would be a silent recalibration wearing the
old version string.

## How "equal" was established

Both implementations were run side by side over a **231-molecule** probe corpus and compared on
three outputs: the `(hydrogen, heavy atom)` acidic set, the basic-nitrogen index set, and the aryl
classification of each basic nitrogen.

The corpus is the `props` solvent table, the `chem` reagent table, and about a hundred hand-written
cases chosen to reach every arm rather than to be representative — thioamides and thioureas (the
chalcogen half of the electron-withdrawing arm), phosphoramides (`N-P=O`, which the arm does *not*
cover, then or now), aromatic N-oxides and quaternary ammonium (charged), azo and imine nitrogen
(double bonds, where the arm needs a single one), isocyanates and isothiocyanates, hydrazides,
peroxides and disulfides, fused azoles, and caffeine — whose N1/N3 are caught by the pyrrole-type
arm rather than the amide one because RDKit gives their bonds aromatic order.

**Zero disagreements, on all three outputs, on the first pattern written.**

That measurement belongs to the commit that made the change and cannot be re-run once the old code
is gone. What survives it is `servers/calc/tests/test_ionisable_sites.py`: a 42-row curated
partition where every row names the arm it exercises, plus a test that drops each exclusion arm in
turn and requires the corpus to then perceive *more* sites — so an arm that has stopped excluding
anything fails rather than becoming untested prose.

One thing the transcription had to preserve deliberately: `_acidic_protons` sorts its matches by
hydrogen index, which is the order the atom walk produced. It decides nothing about the answer — the
most stable anion wins on energy — but it decides which of two exactly degenerate sites is reported,
and a reordering there would be a diff in a stored result with no physics behind it.

## The overlapping question nobody had written down

`servers/chem`'s `engine/species.py` perceives ionisable sites too, and its rules are left alone.
The two are not copies of each other and must not be reconciled: `species.py`'s list is deliberately
**inclusive**, because a microstate nobody enumerated is a form that never reaches a caller;
`pka.py`'s is **narrow and frozen**, for the ledger reason above. They disagree on purpose — an
amide N-H is an acidic site in `species.py` and its nitrogen is never a basic site in `pka.py`. That
is now said in `species.py`'s own table comment, where somebody tempted to "fix" one against the
other will read it. (A third copy lives in Chemclaw3's `science/calc/logd.py` and is pinned to
`pka.py`'s by `servers/calc/tests/test_logd_contract.py`.)

## The secondary defect, and what it is actually worth

`servers/rxnpredict`'s `engine/meta/classifier.py::_any_mol_matches` called `Chem.MolFromSmarts` on
**every** invocation, for every pattern of every rule tried — and `classify_reaction` is called per
reaction by the Mixture-of-Experts trust-prior gating. It is now `@cache`d on the SMARTS string.

**Measured rather than asserted, and it is not large.** An amide-forming reaction, which matches the
first rule and so compiles four patterns: 113 µs → 102 µs (**1.11x**). A reaction that matches
nothing, and so tries every rule: 399 µs → 219 µs (**1.83x**). The honest reading is a worst case
halved, not a hot loop fixed; the reason to do it anyway is that the cost grows with the rule table
while nothing about the call site does.

**The premise that this was the fleet's only per-call compile is false, and the measurement is what
found it.** `servers/chem`'s `species.py::_sites` compiles all eleven of its patterns on every call
— measured on tyrosine at **440 µs** against **31 µs** pre-compiled, out of a 1,443 µs
`enumerate_microstates`, which is a far larger saving than the one taken here. It is a
`docs/BACKLOG.md` row rather than a line in this commit, because `enumerate_microstates` is `chem`'s
heaviest tool and nothing bounds or measures its latency yet; the honest order is the bound first.

## What keeps it true

- `servers/calc/tests/test_ionisable_sites.py::test_the_site_partition_is_the_one_the_calibration_was_fitted_over`
  — the 42-row partition, one row per arm, each naming what it is a case of.
- `servers/calc/tests/test_ionisable_sites.py::test_every_arm_of_the_basic_nitrogen_pattern_excludes_something_in_this_corpus`
  — each exclusion dropped in turn, so an arm that has stopped doing anything is red.
- `servers/calc/tests/test_ionisable_sites.py::test_the_pattern_this_file_asserts_is_the_module_s_own`
  — the re-typed pattern checked against the module's, through RDKit's canonical SMARTS rather than
  as text.
- `servers/calc/tests/test_logd_contract.py::test_ionisable_sites_matches_chemclaw3` — the
  cross-repository half: Chemclaw3's inlined copy still agrees with this one.
- `servers/rxnpredict/tests/test_classifier.py::test_a_pattern_is_compiled_once_per_process_rather_than_once_per_call`
  — counted parses rather than a timing assertion, driven on a reaction that reaches every rule.
- `servers/rxnpredict/tests/test_classifier.py::test_an_unparseable_pattern_is_warned_about_once_and_answers_no_match`
  — the miss path, which must answer "no match" rather than matching everything.
