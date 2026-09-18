# D-2026-09-16-a-number-in-prose-is-a-claim-about-a-commit — A number in prose is a claim about a commit

**Status:** accepted · **Date:** 2026-09-16 · **Commit:** the stale-prose sweep over `6c6a0eb`.
Applies `D-2026-09-14-how-many-stayed-green-is-a-claim-about-a-commit` to three sentences that the
same wave of dependency adoptions falsified and left standing.

## The three, each measured

**1 · `servers/calc/README.md` — "identical to the ones it produced before the split".** The full
sentence claimed "the numbers and the `calc_version` strings this server produces are identical to
the ones it produced before the split — verified by deriving the same keys and the same energies
from both trees". The commit that introduced geomeTRIC falsified it three times in the same hunk: it
bumped `_HAMILTONIAN_REVISION` from `h2` to `h3`, added the scipy distribution to
`engine_version()`, and removed `curvature_floor` from `OptSpec`, which changes the shape of every
optimization key. The line two above it *was* updated in that hunk; this one was not, which is the
tell — a reviewer who edits the neighbouring sentence has read this one and decided it still holds.

What is true is narrower and is now what it says: the *tier* is unchanged, and the key strings are
deliberately this server's own, so a Chemclaw3 row recorded before the split is a miss rather than a
stale hit. The live strings are whatever `test_the_whole_key_is_stable_on_the_versions_this_test_observes`
pins; transcribing them into a README would be stale on the next distribution bump.

**2 · `tests/test_fleet.py::test_the_three_answers_to_molecular_mass_agree` — the spread table.** The
docstring gave the largest spread as "0.011 g/mol (chloroform: 119.38 tabulated, 119.378 from RDKit,
119.369 from `thermalsafety`)". Re-measured over all 44 rows on 2026-09-16, after the weights moved
to `molmass`:

| | then | now |
| --- | --- | --- |
| chloroform, `thermalsafety` | 119.369 | **119.3774** |
| chloroform spread | 0.011 | **0.003** |
| largest spread, any row | chloroform | **dimethyl sulfoxide, 0.006** (78.13 / 78.136 / 78.1333) |

The sentence beside it said `thermalsafety` "carries the IUPAC 2021 conventional 35.45" for
chlorine; the commit that took the table out of this repository carries molmass's **35.4529**, which
is most of why that row moved. The relative figure, 2.8e-4 on water, still reproduces at 2.775e-4
and is left alone.

The same docstring called the check "a four-way agreement written as three comparisons" while the
loop below it builds **two** — four sources produce three numbers, and the vendored `mw` is what the
other two are each checked against. A count of a thing, written beside the thing.

**3 · `servers/calc/tests/test_logd_contract.py` — `_lone_pair_is_available`.** Named as one of the
three parts of the duplicated domain check. It was replaced by a recursive SMARTS on **both** sides
— `_BASIC_NITROGEN` here, `_BASIC_SITE` in `chemclaw/science/calc/logd.py` — and exists in neither
repository. The one document pointing a reader at arithmetic duplicated across a repository boundary
named a third of it by a name nothing answers to.

## Why three corrections and not a rule

The rule already exists and this repository keeps applying it: a live number belongs in the test that
measures it. Two of the three above are now written as dated measurements with the older figures
shown beside them, because "this used to say X" is what stops the next reader from re-transcribing
the old value; the third is a name, which has no dated form and is simply fixed.

None of the three had a test behind it, and none gains one here — a docstring is prose about a
measurement, and the measurement is in the assertion below it.

## What keeps it true

- `tests/test_fleet.py::test_the_three_answers_to_molecular_mass_agree` — the assertion the corrected
  docstring describes, with its 0.05 g/mol tolerance and its floor on rows compared.
- `servers/calc/tests/test_key_contract.py::test_the_whole_key_is_stable_on_the_versions_this_test_observes`
  — what the README now points at instead of transcribing.
- `servers/calc/tests/test_logd_contract.py::test_predict_logd_composition_matches_chemclaw3`
  — the contract the corrected sentence introduces.
