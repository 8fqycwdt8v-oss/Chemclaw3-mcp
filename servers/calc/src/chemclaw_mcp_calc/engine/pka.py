"""xTB-based pKa predictor — and the calculator whose `calc_version` matters most.

**Acids**: the most stable conjugate base over all O-H/S-H sites, its GFN2-xTB ALPB(water)
deprotonation energy mapped to pKa by a linear calibration. Net-neutral inputs only (the fit's
domain); C-H acids are out of scope. Approximate: the calibration residual is the uncertainty.

**Bases**: the reverse (BH+ -> B + H+) for aromatic and aryl nitrogen only, its own calibration.
Aliphatic amines are refused: their aqueous basicity is set by ammonium hydrogen bonding to water,
which a continuum model cannot represent, and the method has no ranking ability for them.

`calc_version` folds in seven settings, the engine build and the relaxation's version; it is the
exact-match key of Chemclaw3's calibration ledger and only this server can produce it.
`predict_pka` canonicalises before computing, because atom order steers the seeded embedding.
"""

from __future__ import annotations

from typing import Literal, NamedTuple

from mcp_server_kit.limits import echo
from pydantic import BaseModel, Field
from rdkit import Chem

from chemclaw_mcp_calc.engine.chem import require_canonical_smiles
from chemclaw_mcp_calc.engine.config import settings
from chemclaw_mcp_calc.engine.key import CalculationKey, Keyed
from chemclaw_mcp_calc.engine.structure import Structure
from chemclaw_mcp_calc.engine.uncertainty import CalculationDomainError
from chemclaw_mcp_calc.engine.xtb_engine import (
    HARTREE_TO_KCAL,
    atom_ceiling_error,
    engine_version,
    geometry,
    gfn2_energy,
    parse_molecule,
    require_closed_shell,
)
from chemclaw_mcp_calc.engine.xtb_opt import OptSpec, optimize_structure

__all__ = [
    "CALC_TYPE",
    "IonisableSites",
    "PkaInput",
    "PkaResult",
    "calc_version",
    "ionisable_sites",
    "pka_cache_key",
    "predict_pka",
    "relaxation_spec",
]

CALC_TYPE = "pka"

# ---------------------------------------------------------------------------------------------
# Site perception, as a SMARTS table.
#
# The calibration was fitted over exactly this enumeration, so a broader or different site set is a
# silent recalibration of every stored residual; these patterns reproduce the bond-walking rules
# they replaced (no disagreements over a 216-molecule probe corpus). Matches go through
# `_all_matches`, which lifts RDKit's silent 1,000-match ceiling.
# ---------------------------------------------------------------------------------------------

#: Every O-H/S-H proton on an explicit-hydrogen molecule, matched as `(hydrogen, heavy atom)`.
#: `X1` keeps a bridging hydride from reading as an acidic proton.
_ACIDIC_PROTON = Chem.MolFromSmarts("[#1X1][#8,#16]")

#: A nitrogen that can accept a proton in water: neutral, with a free valence, excluding
#:
#: - `!$([#7]#*)` — nitrile and other sp nitrogen (pKaH ~ -10);
#: - `!$([nX3])` — pyrrole-type aromatic nitrogen, whose lone pair is in the sextet (pyridine-type,
#:   with two connections, stays);
#: - `!$([#7]-[#6,#16]=[#8,#16])` — amide, carbamate, urea, sulfonamide and thio analogues, which
#:   protonate on oxygen if at all. The single bond keeps aniline (aromatic bond) in.
#:
#: Aromatic amide-like nitrogen (caffeine) is caught by the pyrrole arm. Phosphoramide nitrogen is
#: not excluded.
_BASIC_NITROGEN = Chem.MolFromSmarts(
    "[#7+0;X1,X2,X3;!$([#7]#*);!$([nX3]);!$([#7]-[#6,#16]=[#8,#16])]"
)

#: Aromatic, or attached to an aromatic system — the base calibration's domain, where basicity is
#: dominated by ring delocalisation, which GFN2 with a continuum captures.
_ARYL_NITROGEN = Chem.MolFromSmarts("[$([n]),$([#7]~a)]")


class PkaInput(BaseModel):
    """A pKa request: the neutral acid as SMILES."""

    smiles: str = Field(min_length=1)


class PkaResult(Keyed):
    """A predicted pKa with its uncertainty, and which calibration produced it.

    `deprotonation_energy_kcal` is always the solvated GFN2-xTB energy of the **deprotonated**
    species minus the protonated one — for an acid that is anion minus neutral, for a base neutral
    minus cation. `site` says which, because the number a chemist needs is different: an acid's own
    pKa, or a base's *conjugate acid* pKa.

    `smiles` is the **canonical** form the computation actually ran on, not the caller's spelling —
    which is also what `calc_key`'s `input_hash` was derived from.
    """

    smiles: str
    method: str
    pka: float
    deprotonation_energy_kcal: float
    uncertainty: float
    # "acid": an O-H/S-H proton came off; "base": the conjugate acid's pKa (pKaH). Separate
    # calibrations.
    site: Literal["acid", "base"] = "acid"


def _all_matches(mol: Chem.Mol, pattern: Chem.Mol) -> list[tuple[int, ...]]:
    """Every match of `pattern` in `mol`, with RDKit's silent 1,000-match ceiling lifted.

    Each pattern is anchored on a distinct atom, so the atom count bounds the matches; `max(..., 1)`
    because `maxMatches=0` is not "unlimited".
    """
    matches: list[tuple[int, ...]] = list(
        mol.GetSubstructMatches(pattern, maxMatches=max(mol.GetNumAtoms(), 1))
    )
    return matches


def _acidic_protons(mol: Chem.Mol) -> list[tuple[int, int]]:
    """`(hydrogen index, heavy-atom index)` for every O-H/S-H proton, explicit-H molecule.

    The one definition of an acidic site, shared by `_conjugate_bases` and `ionisable_sites`. Sorted
    by hydrogen index, which decides which of two degenerate sites is reported.
    """
    # `(match[0], match[1])` rather than `match`: `_all_matches` is shared with the two
    # single-atom patterns, so its element type is the widest of the three.
    return sorted((match[0], match[1]) for match in _all_matches(mol, _ACIDIC_PROTON))


def _conjugate_bases(mol: Chem.Mol) -> list[Chem.Mol]:
    """Enumerate deprotonated anions at each acidic O-H/S-H site.

    Removes each hydrogen and puts the -1 charge on the heavy atom, with implicit H disabled so
    sanitising does not re-protonate it.
    """
    anions: list[Chem.Mol] = []
    for h_idx, heavy_idx in _acidic_protons(mol):
        editable = Chem.RWMol(mol)
        heavy = editable.GetAtomWithIdx(heavy_idx)
        heavy.SetFormalCharge(-1)
        heavy.SetNoImplicit(True)
        editable.RemoveAtom(h_idx)
        anion = editable.GetMol()
        Chem.SanitizeMol(anion)
        anions.append(anion)
    return anions


def _basic_nitrogens(mol: Chem.Mol) -> list[int]:
    """Indices of nitrogens that can be protonated — free valence *and* an available lone pair.

    See `_BASIC_NITROGEN` for each exclusion.
    """
    return sorted(match[0] for match in _all_matches(mol, _BASIC_NITROGEN))


class IonisableSites(NamedTuple):
    """How many acid and base sites this predictor's own enumeration finds in a molecule.

    `PkaResult` reports one pKa, so arithmetic that assumes a single equilibrium (logD) needs this
    to know when that assumption is false.
    """

    acidic: int
    basic: int

    @property
    def total(self) -> int:
        """Sites of either kind — the number a single-equilibrium model needs to be 1."""
        return self.acidic + self.basic


def ionisable_sites(smiles: str) -> IonisableSites:
    """Count the acidic O-H/S-H protons and the protonatable nitrogens of a neutral molecule.

    Structural only (no xTB), exactly what `predict_pka` would enumerate; it does not rank sites.
    """
    mol = parse_molecule(smiles)
    return IonisableSites(acidic=len(_acidic_protons(mol)), basic=len(_basic_nitrogens(mol)))


def _aryl_nitrogens(mol: Chem.Mol) -> set[int]:
    """Indices of the nitrogens `_ARYL_NITROGEN` matches — the class the base calibration covers.

    Computed once per molecule rather than per site.
    """
    return {match[0] for match in _all_matches(mol, _ARYL_NITROGEN)}


def _protonated_forms(mol: Chem.Mol, sites: list[int]) -> list[tuple[Chem.Mol, bool]]:
    """Build one cation per basic nitrogen, paired with whether that site is aryl.

    So the caller can pick the most stable protomer and know which calibration applies.
    """
    forms: list[tuple[Chem.Mol, bool]] = []
    aryl_sites = _aryl_nitrogens(mol)
    for index in sites:
        editable = Chem.RWMol(mol)
        nitrogen = editable.GetAtomWithIdx(index)
        aryl = index in aryl_sites
        nitrogen.SetFormalCharge(1)
        nitrogen.SetNumExplicitHs(nitrogen.GetNumExplicitHs() + 1)
        nitrogen.SetNoImplicit(True)
        cation = editable.GetMol()
        try:
            Chem.SanitizeMol(cation)
        except Chem.KekulizeException:  # a protonation that breaks aromaticity is not one
            continue
        forms.append((Chem.AddHs(cation), aryl))
    return forms


def relaxation_spec() -> OptSpec:
    """The optimizer the base branch relaxes both species with — built in exactly one place.

    `pka_cache_key` uses it too, so the key describes the spec that actually runs.
    """
    return OptSpec(solvent=settings.pka_solvent)


def _relaxed_energy(mol: Chem.Mol, charge: int) -> float:
    """Solvated energy of `mol` at a GFN2-optimized geometry.

    The base path optimises because protonation reshapes nitrogen geometry (the base calibration was
    fitted this way); the acid calibration keeps its force-field geometry policy.
    """
    numbers, positions = geometry(mol, settings.xtb_embed_seed, optimize=True)
    structure = Structure(
        elements=[int(number) for number in numbers],
        positions=[[float(value) for value in row] for row in positions],
        charge=charge,
    )
    return optimize_structure(relaxation_spec(), structure).energy_hartree


def _predict_base_pka(
    smiles: str, base: Chem.Mol, sites: list[int], version: str, key: str
) -> PkaResult:
    """Predict the conjugate-acid pKa (pKaH) of a base, for aromatic/aryl nitrogen only.

    Raises for an aliphatic amine: the method has no ranking ability there, so a number would only
    look like information.
    """
    forms = _protonated_forms(base, sites)
    if not forms:
        raise CalculationDomainError(f"no protonatable nitrogen in {echo(smiles)!r}")
    energy_base = _relaxed_energy(base, charge=0)
    # The conjugate acid is the *most stable* protomer, so it is the lowest energy that defines the
    # equilibrium — and its site decides which calibration applies.
    energy_cation, aryl = min(
        ((_relaxed_energy(cation, charge=1), aryl) for cation, aryl in forms),
        key=lambda pair: pair[0],
    )
    if not aryl:
        raise CalculationDomainError(
            f"{echo(smiles)!r} protonates on an aliphatic nitrogen, which this predictor does "
            "not cover: over 13 reference amines its computed basicity correlates with "
            "the measured pKa at Spearman -0.17 (no ranking ability). The cause is the "
            "implicit solvent — aqueous aliphatic amine basicity is set by the ammonium "
            "ion's hydrogen bonding to water, which a continuum model cannot represent"
        )
    delta_e_kcal = (energy_base - energy_cation) * HARTREE_TO_KCAL
    return PkaResult(
        calc_version=version,
        calc_key=key,
        smiles=smiles,
        method=f"{settings.xtb_method}/ALPB-{settings.pka_solvent}",
        pka=settings.pka_base_calibration_slope * delta_e_kcal
        + settings.pka_base_calibration_intercept,
        deprotonation_energy_kcal=delta_e_kcal,
        uncertainty=settings.pka_base_uncertainty,
        site="base",
    )


def _predict_acid_pka(
    smiles: str, acid: Chem.Mol, anions: list[Chem.Mol], version: str, key: str
) -> PkaResult:
    """Predict the pKa of a neutral O-H/S-H acid from its most stable conjugate base.

    Acid and anions share one geometry policy (MMFF where parametrised, else the embedded geometry),
    the path the calibration was fitted through; the lowest-energy anion defines the site.
    """
    numbers, positions = geometry(acid, settings.xtb_embed_seed, optimize=True)
    energy_acid = gfn2_energy(settings.xtb_method, numbers, positions, solvent=settings.pka_solvent)

    best_anion_energy = min(
        gfn2_energy(
            settings.xtb_method,
            *geometry(anion, settings.xtb_embed_seed, optimize=True),
            charge=-1,
            solvent=settings.pka_solvent,
        )
        for anion in anions
    )

    delta_e_kcal = (best_anion_energy - energy_acid) * HARTREE_TO_KCAL
    pka = settings.pka_calibration_slope * delta_e_kcal + settings.pka_calibration_intercept
    return PkaResult(
        calc_version=version,
        calc_key=key,
        smiles=smiles,
        method=f"{settings.xtb_method}/ALPB-{settings.pka_solvent}",
        pka=pka,
        deprotonation_energy_kcal=delta_e_kcal,
        uncertainty=settings.pka_uncertainty,
    )


def predict_pka(job: PkaInput) -> PkaResult:
    """Predict the pKa of the most acidic O-H/S-H site of a neutral molecule, or a base's pKaH.

    Canonicalises first, because atom order steers the seeded embedding and the key must name what
    was computed. Raises `ValueError` for an unparseable SMILES, a net-charged or open-shell input
    (outside the calibration domain), or a molecule with no acidic O-H/S-H and no basic nitrogen.
    """
    canonical = require_canonical_smiles(job.smiles)
    version = calc_version()
    key = pka_cache_key(PkaInput(smiles=canonical)).as_str()

    neutral = parse_molecule(canonical)
    # Refused up front so the message names the caller's SMILES; `geometry()` and
    # `make_calculator()` enforce the ceiling too, including on the one-atom-larger protonated form.
    if reason := atom_ceiling_error(
        neutral.GetNumAtoms(), subject=f"the molecule {echo(job.smiles)!r}"
    ):
        raise ValueError(reason)
    formal_charge = Chem.GetFormalCharge(neutral)
    if formal_charge != 0:
        raise CalculationDomainError(
            f"pKa requires a neutral acid; {echo(job.smiles)!r} has net formal charge "
            f"{formal_charge}"
        )
    require_closed_shell(neutral, 0)
    anions = _conjugate_bases(neutral)
    if anions:
        return _predict_acid_pka(canonical, neutral, anions, version, key)

    # No proton to lose, but possibly a lone pair to gain one. Acid wins when both exist: that is
    # "the pKa" of, say, an aminophenol.
    basic = _basic_nitrogens(neutral)
    if basic:
        return _predict_base_pka(canonical, neutral, basic, version, key)
    raise CalculationDomainError(
        f"no acidic O-H/S-H site and no basic nitrogen in {echo(job.smiles)!r}: nothing to "
        "protonate or deprotonate"
    )


def calc_version() -> str:
    """The version this calculator's results are keyed and **calibrated** under.

    Names the method, engine build, solvent, both calibrations and both uncertainties (the
    uncertainty is part of the result), plus the relaxation's own version unconditionally — the
    branch is chosen after the version is built. Moving this string makes every reconciled residual
    in Chemclaw3's ledger unreachable, and under `xtb_engine=auto` pods with and without the binary
    deliberately differ; pin `CHEMCLAW_XTB_ENGINE` to avoid that split.
    """
    return (
        f"{settings.xtb_method}+{engine_version()}/alpb-{settings.pka_solvent}/"
        f"cal-{settings.pka_calibration_slope}:{settings.pka_calibration_intercept}/"
        f"base-{settings.pka_base_calibration_slope}:{settings.pka_base_calibration_intercept}/"
        f"u-{settings.pka_uncertainty}:{settings.pka_base_uncertainty}/"
        f"opt-{relaxation_spec().calc_version()}"
    )


def pka_cache_key(job: PkaInput) -> CalculationKey:
    """The versioned identity of predicting `job`'s pKa.

    Expects canonical SMILES (`predict_pka` canonicalises first). The relaxation's knobs go in
    `params`, as `XtbSpec.cache_key` does, because they move the answer.
    """
    spec = relaxation_spec()
    return CalculationKey.build(
        calc_type=CALC_TYPE,
        calc_version=calc_version(),
        inputs={"smiles": job.smiles},
        params={
            "embed_seed": settings.xtb_embed_seed,
            "opt": spec.model_dump(exclude=spec.unkeyed_fields()),
        },
    )
