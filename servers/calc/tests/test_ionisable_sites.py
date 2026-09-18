"""Ionisable-site perception: the partition `pka.calc_version()`'s calibration was fitted over.

`engine/pka.py`'s site rules were fifty lines of `GetBonds()` walking and are now a SMARTS table.
**That was a transcription, not a redesign**, and this file is what holds it to that: the
calibration ledger on Chemclaw3's side matches `calc_version` exactly with no version pooling, so a
site rule that perceives one extra nitrogen picks a different most-stable protomer on some molecule
and silently invalidates every residual recorded against that version. It is also why
`dimorphite-dl` was declined — a broader site set is exactly what must not happen here.

**How the transcription was proven, and why that proof is not this file.** Both implementations were
run side by side over a 231-molecule probe corpus — the `props` solvent table, the `chem` reagent
table, and ~100 hand-written cases chosen to reach every arm — with zero disagreements on the acidic
set, the basic set and the aryl classification. That measurement belongs to the commit that made the
change; it cannot be re-run once the old code is gone. What survives it is the *partition*, pinned
below on a curated set where every row names the arm it exercises, so a future edit to the table
fails here with a molecule a chemist can reason about rather than with a count.

Each row is `(smiles, acidic sites, basic sites, what it is a case of)`. The counts are structural:
`ionisable_sites` reports what `predict_pka` would enumerate before any xTB runs, so nothing in this
file is noisy and nothing in it needs a calculator.
"""

from __future__ import annotations

import pytest
from chemclaw_mcp_calc.engine.pka import ionisable_sites

#: One row per arm of the SMARTS table, plus the acidic cases that fix the O-H/S-H half.
PARTITION: list[tuple[str, int, int, str]] = [
    ("CC(=O)N", 0, 0, "acetamide: the amide arm — C=O drains the lone pair"),
    ("NC(=O)N", 0, 0, "urea: both nitrogens on the same C=O"),
    ("COC(=O)N", 0, 0, "carbamate"),
    ("CS(=O)(=O)N", 0, 0, "sulfonamide: the S=O half of the electron-withdrawing arm"),
    ("CS(=O)N", 0, 0, "sulfinamide"),
    ("CC(=S)N", 0, 0, "thioamide: the chalcogen arm is O *or* S"),
    ("NC(=S)N", 0, 0, "thiourea"),
    ("CC#N", 0, 0, "nitrile: the sp arm"),
    ("N#Cc1ccncc1", 0, 1, "4-cyanopyridine: sp N excluded, ring N kept"),
    ("c1cc[nH]c1", 0, 0, "pyrrole: pyrrole-type aromatic N"),
    ("c1c[nH]cn1", 0, 1, "imidazole: one of each type in one ring"),
    ("c1ccncc1", 0, 1, "pyridine: pyridine-type aromatic N"),
    (
        "Cn1cnc2c1c(=O)n(C)c(=O)n2C",
        0,
        1,
        "caffeine: the amide-in-a-ring case the pyrrole arm catches",
    ),
    ("Nc1ccccc1", 0, 1, "aniline: aromatic bond, so the amide arm does not reach it"),
    ("CC(=O)Nc1ccc(O)cc1", 1, 0, "paracetamol: amide N excluded, phenol O-H counted"),
    ("CCN(CC)CC", 0, 1, "triethylamine: an aliphatic amine is a site here and refused downstream"),
    ("NP(=O)(N)N", 0, 3, "phosphoramide: N-P=O is *not* excluded, as before"),
    ("CN=C", 0, 1, "imine: a double bond is not the single bond the amide arm needs"),
    ("CN=NC", 0, 2, "azo"),
    ("C[N+](C)(C)C", 0, 0, "quaternary ammonium: charged"),
    ("c1cc[n+]([O-])cc1", 0, 0, "pyridine N-oxide: charged"),
    ("CN=C=O", 0, 1, "methyl isocyanate: cumulated double bonds, no single bond to a C=O"),
    ("CC(=O)NN", 0, 1, "acetohydrazide: one N excluded by the amide arm, one kept"),
    ("O=C(O)c1ccccc1", 1, 0, "benzoic acid"),
    ("Oc1ccccc1", 1, 0, "phenol"),
    ("CC(=O)O", 1, 0, "acetic acid"),
    ("OCCO", 2, 0, "ethylene glycol: diprotic"),
    ("O=C(O)CCC(=O)O", 2, 0, "succinic acid: diprotic"),
    ("SCCO", 2, 0, "2-mercaptoethanol: S-H and O-H are both acidic sites"),
    ("CS", 1, 0, "methanethiol"),
    ("O=S(=O)(O)c1ccccc1", 1, 0, "benzenesulfonic acid"),
    ("O=P(O)(O)O", 3, 0, "phosphoric acid: triprotic"),
    ("OO", 2, 0, "hydrogen peroxide"),
    ("CC(=O)[O-]", 0, 0, "acetate: charged, nothing left"),
    ("NCC(=O)O", 1, 1, "glycine: amphoteric"),
    ("NC(CS)C(=O)O", 2, 1, "cysteine: two acidic sites and a basic one"),
    ("NC(Cc1c[nH]cn1)C(=O)O", 1, 2, "histidine: imidazole beside an amino acid"),
    ("O=C1NC(=O)NC1=O", 0, 0, "parabanic acid: every N is an amide N"),
    ("c1ccc2[nH]ncc2c1", 0, 1, "indazole: pyrrole-type and pyridine-type in a fused ring"),
    ("ON=C(C)C", 1, 1, "acetone oxime: the O-H is acidic, the C=N nitrogen is basic"),
    ("c1ccc2c(c1)[nH]c1ccccc12", 0, 0, "carbazole"),
    ("NO", 1, 1, "hydroxylamine"),
]


@pytest.mark.parametrize(
    ("smiles", "acidic", "basic", "why"), PARTITION, ids=[row[0] for row in PARTITION]
)
def test_the_site_partition_is_the_one_the_calibration_was_fitted_over(
    smiles: str, acidic: int, basic: int, why: str
) -> None:
    """One molecule, one arm of the table. A changed count is a changed calibration."""
    sites = ionisable_sites(smiles)
    assert (sites.acidic, sites.basic) == (acidic, basic), why


#: The basic-nitrogen pattern, split into the part that selects and the three arms that exclude, so
#: the test below can drive it with one arm removed. Re-typed from `engine/pka.py` on purpose and
#: then checked against it — see `test_the_pattern_this_file_asserts_is_the_module_s_own`.
_SELECTOR = "[#7+0;X1,X2,X3"
_ARMS = ("!$([#7]#*)", "!$([nX3])", "!$([#7]-[#6,#16]=[#8,#16])")


def _pattern(arms: tuple[str, ...]) -> str:
    """The pattern the selector and `arms` spell."""
    return _SELECTOR + "".join(f";{arm}" for arm in arms) + "]"


def test_every_arm_of_the_basic_nitrogen_pattern_excludes_something_in_this_corpus() -> None:
    """A pattern arm that excludes nothing is an arm nobody would notice losing.

    The three exclusions are what make this enumeration narrower than "every nitrogen with a free
    valence", and the corpus above has to reach all three or two thirds of the table is untested
    prose. Checked by construction: each arm is dropped in turn and the corpus must then perceive
    **more** basic sites than it does with the whole pattern.
    """
    from chemclaw_mcp_calc.engine.xtb_engine import parse_molecule
    from rdkit import Chem

    def perceived(pattern: str) -> int:
        compiled = Chem.MolFromSmarts(pattern)
        assert compiled is not None, f"{pattern} is not a SMARTS"
        return sum(len(parse_molecule(row[0]).GetSubstructMatches(compiled)) for row in PARTITION)

    baseline = perceived(_pattern(_ARMS))
    assert baseline == sum(row[2] for row in PARTITION), (
        "the pattern asserted here is not the one the module uses"
    )
    for dropped in _ARMS:
        widened = _pattern(tuple(arm for arm in _ARMS if arm != dropped))
        assert perceived(widened) > baseline, (
            f"dropping {dropped!r} from the basic-nitrogen pattern changes nothing over this "
            "corpus, so that arm is untested — add a molecule it excludes"
        )


def test_the_pattern_this_file_asserts_is_the_module_s_own() -> None:
    """The arm test above re-types the pattern, which is the shape that asserts nothing.

    `D-2026-09-12-a-test-that-re-types-the-expression-under-test-asserts-nothing` is the record. The
    re-typed string is unavoidable there — the point is to drive *modified* copies of it — so the
    honest arrangement is to check the unmodified one against the module, through RDKit's own
    canonical SMARTS rather than as text.
    """
    from chemclaw_mcp_calc.engine import pka
    from rdkit import Chem

    mine = Chem.MolFromSmarts(_pattern(_ARMS))
    assert mine is not None
    assert Chem.MolToSmarts(pka._BASIC_NITROGEN) == Chem.MolToSmarts(mine)


def test_a_molecule_with_more_sites_than_rdkit_s_default_ceiling_is_counted_whole() -> None:
    """`GetSubstructMatches` stops at 1,000 matches and says nothing about having stopped.

    The imperative walk these SMARTS replaced had no bound, so the transcription introduced one —
    invisibly, because every molecule in the 231-row probe corpus above has fewer than ten sites
    and could not reach it (`D-2026-09-16-a-default-ceiling-is-a-silent-truncation`).

    **A nitrogen chain, because it is the cheapest molecule that crosses the ceiling while staying
    inside every bound this server enforces**: 1,500 heavy atoms against
    `MAX_MOLECULE_ATOMS`'s 2,000 and 1,500 characters against `MAX_SMILES_CHARS`'s 4,000. Measured
    at `6c6a0eb`, `ionisable_sites` reported `basic=1000` for it. The count is asserted against
    the molecule's own nitrogen count rather than against the literal 1,500, so the assertion is
    about the perception being complete rather than about this string.

    The published tool surface does not reach this — `predict_logd` is the only caller and its pKa
    refuses above `xtb_max_atoms` first — which is why it is worth a test and not a release note:
    an unreachable truncation is one that becomes reachable the day a bound moves, and nothing
    would have said so.
    """
    from chemclaw_mcp_calc.engine.pka import _acidic_protons, _basic_nitrogens
    from chemclaw_mcp_calc.engine.xtb_engine import parse_molecule
    from mcp_server_kit.limits import MAX_MOLECULE_ATOMS, MAX_SMILES_CHARS

    chain = "N" * 1_500
    assert len(chain) <= MAX_SMILES_CHARS
    molecule = parse_molecule(chain)
    nitrogens = sum(1 for atom in molecule.GetAtoms() if atom.GetSymbol() == "N")
    assert nitrogens <= MAX_MOLECULE_ATOMS, "the probe must stay inside the bound this server sets"
    assert nitrogens > 1_000, "the probe no longer crosses RDKit's default ceiling"
    assert len(_basic_nitrogens(molecule)) == nitrogens
    assert ionisable_sites(chain).basic == nitrogens

    hydroxyls = "C" + "C(O)" * 1_100 + "C"
    assert len(_acidic_protons(parse_molecule(hydroxyls))) == 1_100
