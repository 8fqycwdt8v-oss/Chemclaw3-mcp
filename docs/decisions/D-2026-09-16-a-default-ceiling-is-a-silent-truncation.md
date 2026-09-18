# D-2026-09-16-a-default-ceiling-is-a-silent-truncation — A default ceiling is a silent truncation

**Status:** accepted · **Date:** 2026-09-16 · **Commit:** the `pka` match-ceiling lift. Extends
`D-2026-09-16-a-transcription-is-proven-equal-not-argued-better`, whose equality proof was taken
below a ceiling neither implementation mentioned.

## What was measured

`Chem.Mol.GetSubstructMatches` stops at **1,000** matches by default and reports nothing about
having stopped: no warning, no flag on the result, a short tuple that looks like a complete one. The
~50 lines of `GetBonds()` walking it replaced had no bound at all.

```
_basic_nitrogens(parse_molecule("N" * 1500))   -> 1000   (the walk counts 1500)
ionisable_sites("N" * 1500)                    -> IonisableSites(acidic=0, basic=1000)
_acidic_protons(parse_molecule("C" + "C(O)"*1100 + "C"))  -> 1000   (1100 O-H protons)
```

The nitrogen chain is inside every bound this server enforces on a caller's string —
`mcp_server_kit.limits.MAX_MOLECULE_ATOMS` is 2,000 and `MAX_SMILES_CHARS` is 4,000.

The 216-molecule agreement between the two implementations stands and is unaffected: every probe in
it has fewer than ten sites, which is exactly why the ceiling could not appear in it. A proof taken
on small molecules is a proof about small molecules.

## Reachability, and why it is fixed anyway

It is **not** reachable through the published tool surface. `predict_logd` is the only tool-level
caller of `ionisable_sites`, and it runs a pKa first, whose `Structure` refuses above
`xtb_max_atoms` (500) — so the truncated count never reaches a caller.

Fixed regardless, because "unreachable" here is a property of a *different* module's constant. The
day `xtb_max_atoms` moves, or a tool exposes site perception directly, the truncation becomes
reachable and nothing would say so. The comment block above the SMARTS also claimed a transcription
"proven equal" with no mention of a ceiling, which is the claim this record exists to correct.

## The bound, derived rather than chosen

`_all_matches(mol, pattern)` passes `maxMatches=max(mol.GetNumAtoms(), 1)`. Every pattern here is
anchored on a distinct atom — a hydrogen for `_ACIDIC_PROTON`, a nitrogen for `_BASIC_NITROGEN` and
`_ARYL_NITROGEN` — so the match count cannot exceed the atom count, and the atom count is therefore
a bound that comes from the molecule instead of from a constant somebody has to keep in step with
`MAX_MOLECULE_ATOMS`. `max(..., 1)` because `maxMatches=0` is not "no limit" in RDKit's API.

Three call sites, one helper: the Rule of Three, exactly met.

## What keeps it true

- `servers/calc/tests/test_ionisable_sites.py::test_a_molecule_with_more_sites_than_rdkit_s_default_ceiling_is_counted_whole`
  — asserts the count against the molecule's own nitrogen count rather than against the literal
  1,500, so the assertion is about the perception being complete rather than about the probe string,
  and asserts the probe still crosses the ceiling so it cannot go quiet.
- `servers/calc/tests/test_ionisable_sites.py::test_the_site_partition_is_the_one_the_calibration_was_fitted_over`
  — the partition the ceiling was hiding inside, unchanged.
