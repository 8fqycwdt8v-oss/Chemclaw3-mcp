# D-2026-09-16-a-derivable-number-the-fleet-held-four-times — A derivable number the fleet held four times

**Status:** accepted · **Date:** 2026-09-16 · **Commit:** the molecular-mass reconciliation.
Supersedes nothing; it extends `tests/test_fleet.py`'s density check to the harder case the same
argument covers, and deletes the copy that made the case obvious.

## The gap

`tests/test_fleet.py::test_the_two_tables_that_both_hold_densities_agree` exists because two servers
carrying a density for THF is a fact neither of them can see. Molecular mass is the same fact and
the fleet held it **four** times, with nothing reconciling any pair:

- `servers/props` vendors an `mw` column, hand-compiled with the rest of the solvent sheet.
- `servers/props` vendors a `formula` column beside it, written independently of that `mw`.
- `servers/chem`'s `engine/chem.py::molecular_weight` computes RDKit's `Descriptors.MolWt` from a
  SMILES — which, since the SMILES column was compiled beside the formula rather than derived from
  it, is a third independent statement of the molecule.
- `servers/thermalsafety`'s `engine/oxygen_balance.py::molar_mass` derives one from a formula with
  no cheminformatics toolkit at all, and that number divides into every oxygen balance it reports.

A fifth copy sat in `servers/props/tests/test_dataset.py`: a six-element `ATOMIC_WEIGHTS` dict and a
`_formula_weight` tokenizer, written so that the formula column could be checked against the `mw`
column without a dependency. It was the smallest of the four periodic tables in this repository and
the only one whose sole purpose was to make a check possible that the fleet could make better.

## What is checked, and why the mass case is stronger than the density case

Densities are compared with a 1% tolerance because they are handbook values at "20-25 °C" and the
two tables may have taken different temperatures. A mass has no such freedom: the three routes must
agree to within the rounding of the atomic-weight table each one used, and nothing else.

Measured over all 44 solvent rows at the commit that added the check:

- the largest spread between the three answers for one compound is **0.011 g/mol** — chloroform,
  119.38 tabulated, 119.378 from RDKit, 119.369 from `thermalsafety`;
- the largest *relative* spread is **2.8e-4** — water, whose `mw` is rounded to 18.02;
- the disagreement is entirely rounding of the standard atomic weights: RDKit carries chlorine at
  35.453 where `thermalsafety` carries the IUPAC 2021 conventional 35.45.

The tolerance is **0.05 g/mol**, which is what `servers/props`' own deleted check used. It is four
times the observed spread and an order of magnitude below the ~1 g/mol a transposed subscript or a
missing hydrogen moves a mass by, which is the failure the check exists for.

## The copy that was deleted, and what the module now says instead

`servers/props/tests/test_dataset.py` no longer carries a periodic table, and its module docstring
no longer lists "formula against molecular weight" among the checks it makes — it names where that
check went. That matters more than the deletion: a docstring claiming a check a file no longer makes
is the shape this repository keeps finding and deleting, and a reviewer reading the header is
reading it to find out what the corpus is protected by.

The check did not merely move; it got stronger. It used to compare one column against a local
constant. It now compares three servers, and a transposed digit in **either** the `formula` or the
`mw` column fails it — as does a `thermalsafety` weight table that stops agreeing with RDKit's.

## What keeps it true

- `tests/test_fleet.py::test_the_three_answers_to_molecular_mass_agree` — the reconciliation itself,
  with the 0.05 g/mol tolerance and a floor on how many rows were compared, so a table that loses a
  column fails rather than passing vacuously.
- `tests/test_fleet.py::test_the_two_tables_that_both_hold_densities_agree` — the sibling this was
  written in the shape of, named here because the two must stay recognisably one pattern.
- `servers/thermalsafety/tests/test_oxygen_balance.py::test_every_weight_in_the_table_is_a_plausible_atomic_mass`
  — the weights one leg of the reconciliation is built on, checked against an independently written
  statement of six of them.
