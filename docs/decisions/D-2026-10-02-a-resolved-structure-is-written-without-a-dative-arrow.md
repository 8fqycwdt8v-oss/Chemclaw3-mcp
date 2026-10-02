# D-2026-10-02-a-resolved-structure-is-written-without-a-dative-arrow — A resolved structure is written without a dative arrow

**Status:** accepted · **Date:** 2026-10-02 · **Scope:** `servers/chem`, `resolve_compound` (and
the charge table, which resolves through it).

## What was found

- **RDKit writes a metal-ligand bond as an arrow the agent cannot carry.** The sanitizer turns a
  single bond from an over-valent donor to a metal (the `[NH2]` of a Buchwald palladacycle, the
  `[NH3]` of cisplatin) into a *dative* bond, and `MolToSmiles` spells it `<-`/`->`. In the live
  re-verification of 2026-10-02, `resolve_compound` on a Josiphos-type Pd G3 precatalyst returned
  `...[Pd]2(<-[NH2]c3ccccc3...)...`. The agent called that "the standardized structure", re-typed
  it as `<-NH2` (brackets lost), got `unparseable SMILES` from `similar_molecules` twice, and told
  the chemist first that the compound id would change and then that it would not.
- **The structure itself was never wrong, and the id never moved.** Measured with Chemclaw3's own
  functions on its main branch the same day (std12): the raw input, the arrow spelling and the
  spelling below all give one `compound_id`, and Chemclaw3's `require_canonical_smiles` returns the
  spelling below unchanged. The defect was the notation, not the identity.

## What was decided

- **`resolve_compound` returns each dative bond as a charge-separated single bond**
  (`[Pd-]...[NH2+]`, `[Pt-2]([NH3+])...`, `[n+]` beside `[Pd-2]`), through
  `engine/chem.py::require_dative_free_smiles`. Hydrogen counts on both ends are held, so the atoms
  and the molecular weight are unchanged; the output parses with no rewritable arrow left, so
  resolving the answer returns the answer.
- **Charge-separated, not a plain single bond**, because a plain single bond does not survive every
  donor: a pyridine nitrogen on palladium becomes a four-valent aromatic `n` that RDKit refuses.
- **A π-donor keeps its arrow.** An alkene or arene drawn as a donor would need a four-bonded
  carbocation or a non-kekulizable ring; a σ metal-carbon bond would be a different compound.
  Each bond is decided on its own, so one π-donor does not keep the others' arrows.
- **`require_canonical_smiles` is unchanged.** It is a copy of Chemclaw3's, and its contract table
  must pass in both repositories; Chemclaw3's copy still writes the arrow for these inputs. The new
  spelling is a separate function with its own table, whose agreement with Chemclaw3 is the
  measured pair above (same string back from its canonicalizer, same std12 id), recorded as data
  beside the test rather than imported.

## Alternatives weighed

- **Return Chemclaw3's std12 standard form** (metals disconnected, `[Pd+]`, `[Fe+2]`,
  cyclopentadienides). Declined: that answers "is this the same compound?", and `resolve_compound`
  answers "what structure did the chemist mean?" — a disconnected salt is not the precatalyst a
  charge table weighs, and porting `standardize` here is what `engine/chem.py` deliberately left
  behind.
- **Change `require_canonical_smiles` in both repositories.** Possible, and not needed for this
  defect: no key is derived here, Chemclaw3 already accepts the new spelling as canonical, and its
  compound id does not move. It would be a Chemclaw3 change first, argued there.

## What keeps it true

- `servers/chem/tests/test_canonicalization_contract.py::test_a_dative_bond_is_returned_charge_separated`
- `servers/chem/tests/test_canonicalization_contract.py::test_the_returned_spelling_is_a_fixed_point`
- `servers/chem/tests/test_canonicalization_contract.py::test_the_rewrite_moves_no_atom_and_no_hydrogen`
- `servers/chem/tests/test_canonicalization_contract.py::test_without_a_dative_bond_the_two_spellings_are_one`
- `servers/chem/tests/test_tools.py::test_a_metal_complex_resolves_to_a_spelling_that_survives_resubmission`
