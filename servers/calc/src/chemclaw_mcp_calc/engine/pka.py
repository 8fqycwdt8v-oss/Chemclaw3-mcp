"""xTB-based pKa predictor — and the calculator whose `calc_version` matters most.

The standard free-energy-difference approach at semiempirical level: for the most acidic O-H/S-H
site, compute the GFN2-xTB solvated (ALPB water) deprotonation energy and map it to pKa with a
linear calibration (slope/intercept from config). Candidate sites are enumerated, each conjugate
base is evaluated, and the most stable anion defines the pKa.

Approximate by construction — the result carries the calibration's residual as an uncertainty; never
present the value as exact. Covers **net-neutral** O-H/S-H acids (carboxylic acids, phenols,
alcohols, thiols); the calibration was fitted over neutral reference acids through this exact
acid(0)/anion(-1) path, so charged inputs are rejected rather than mapped through an out-of-domain
calibration. C-H acids are out of scope.

**Bases.** The same construction runs in reverse: enumerate the protonated forms, take the most
stable, and calibrate the energy of BH+ -> B + H+ to the conjugate-acid pKa. Fitted over 20
experimental amines, and the fit split the class in two so sharply that only one half ships:

- **Aromatic and aryl nitrogen** — pyridines, imidazoles, azoles, anilines. Spearman **1.000** over
  seven compounds spanning pKa 1.0-6.95, R^2 0.993, worst error -0.37. Better than the acid
  calibration, and shipped.
- **Aliphatic amines** — refused. Spearman **-0.17**: the method does not merely predict them
  imprecisely, it has no ranking ability at all, and a number would be worse than a refusal because
  it would look like an answer.

The failure is diagnosed rather than assumed, and the diagnosis is why no amount of recalibration
fixes it. In the **gas phase** GFN2 reproduces the experimental proton affinities exactly
(NH3 < MeNH2 < Me2NH < Me3N), so the Hamiltonian is fine. Switching on ALPB **reverses** that order
completely. And the true aqueous order is neither: it is non-monotonic
(Me3N < NH3 < MeNH2 < Me2NH), because aqueous aliphatic amine basicity is set by how many hydrogen
bonds the ammonium ion can donate to water — which falls with substitution and which a continuum
model, having no explicit solvent, cannot represent. A different linear map cannot recover a
non-monotonic relationship.

## Why this module is the reason the port has a critical requirement

`calc_version()` interpolates **seven** `settings.*` values — both calibrations, both uncertainties
and the solvent — plus `engine_version()` (tblite + RDKit distributions) plus the relaxation's own
`OptSpec.calc_version()`, which resolves the backend and therefore may shell out to `xtb --version`.
That string is the primary key of Chemclaw3's calibration ledger: `predictions` is unique on
`(calc_type, calc_version, input_hash)` and `reconciled_for` matches it **exactly**, with no version
pooling, so a version string that does not match the one the ledger was filled under makes every
recorded residual unreachable and `calculator_trust("pka")` report `UNCALIBRATED`, n=0.

A Chemclaw3 pod after the split can compute none of it. So `predict_pka` returns it, and
`predict_pka` returns `calc_key` too.

**Ported without `run_cached_pka`.** Its one behaviour beyond the store lookup — canonicalize the
SMILES *before* computing, because atom order steers the seeded embedding — has moved into
`predict_pka` itself, so a caller cannot get it wrong by taking the uncached entry point.
"""

from __future__ import annotations

from typing import Literal, NamedTuple

from pydantic import BaseModel, Field
from rdkit import Chem

from chemclaw_mcp_calc.engine.chem import require_canonical_smiles
from chemclaw_mcp_calc.engine.config import settings
from chemclaw_mcp_calc.engine.key import CalculationKey, Keyed
from chemclaw_mcp_calc.engine.structure import Structure
from chemclaw_mcp_calc.engine.uncertainty import CalculationDomainError
from chemclaw_mcp_calc.engine.xtb_engine import (
    HARTREE_TO_KCAL,
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
# Heavy atoms whose O-H/S-H protons we treat as acidic sites.
_ACIDIC_HEAVY = (8, 16)  # O, S
# Nitrogen valence at which there is no lone pair left to protonate.
_SATURATED_NITROGEN = 4
# Sigma bonds at which an *aromatic* nitrogen's lone pair has gone into the ring's pi system instead
# of staying in an in-plane orbital: pyrrole-type rather than pyridine-type.
_PYRROLE_TYPE_SIGMA_BONDS = 3
# Atoms that drain an adjacent nitrogen's lone pair when they carry a double bond to a chalcogen:
# carbon (amide, carbamate, urea) and sulfur (sulfonamide, sulfinamide).
_ELECTRON_WITHDRAWING = (6, 16)  # C, S
# The chalcogen on the far end of that double bond.
_CHALCOGEN = (8, 16)  # O, S


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
    # "acid" = an O-H/S-H proton came off; "base" = the pKa of the protonated form (pKaH), which is
    # what is tabulated for amines and what an extraction pH is set against. Each has its own
    # calibration, fitted separately.
    site: Literal["acid", "base"] = "acid"


def _acidic_protons(mol: Chem.Mol) -> list[tuple[int, int]]:
    """`(hydrogen index, heavy-atom index)` for every O-H/S-H proton, explicit-H molecule.

    The module's one definition of "acidic site": `_conjugate_bases` deprotonates exactly these and
    `ionisable_sites` counts exactly these, so a caller asking *how many* sites a molecule has
    cannot disagree with the enumeration that produced the pKa.
    """
    return [
        (atom.GetIdx(), atom.GetNeighbors()[0].GetIdx())
        for atom in mol.GetAtoms()
        if atom.GetAtomicNum() == 1
        and atom.GetDegree() == 1
        and atom.GetNeighbors()[0].GetAtomicNum() in _ACIDIC_HEAVY
    ]


def _conjugate_bases(mol: Chem.Mol) -> list[Chem.Mol]:
    """Enumerate deprotonated anions at each acidic O-H/S-H site.

    For every hydrogen bonded to O or S, remove it and place the -1 charge on the heavy atom (with
    implicit H disabled so the anion is not silently re-protonated on sanitize). Returns one
    sanitized anion molecule per candidate site.
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


def _lone_pair_is_available(atom: Chem.Atom) -> bool:
    """Whether this nitrogen's lone pair can actually accept a proton in water.

    Free valence says a lone pair *exists*; it does not say the pair is available, and three common
    classes have one that is not. They are excluded here rather than left for a downstream caller to
    second-guess, because `_predict_base_pka` will otherwise compute and report a conjugate-acid pKa
    for a molecule that has no basic centre at all — a confident number on exactly the class where
    it is most wrong.

    - **Amide, carbamate, urea, sulfonamide** — a nitrogen single-bonded to a carbon or sulfur that
      carries a double bond to O or S. The lone pair is conjugated into that C=O/S=O, and the
      consequence is not a shifted pKa but a different molecule: protonated acetamide has pKaH ~
      -0.5 **and protonates on the oxygen**, so the nitrogen this enumeration would offer is not the
      site even in the strongest acid.
    - **Nitrile** — an sp nitrogen (a triple bond). pKaH ~ -10; there is no aqueous pH at which any
      of it is protonated.
    - **Pyrrole-type aromatic nitrogen** — an aromatic nitrogen with three sigma bonds, so its lone
      pair is the ring's aromatic sextet rather than an in-plane orbital. Pyrrole's pKaH is ~ -4,
      and protonating it costs the ring its aromaticity. The **pyridine-type** nitrogen beside it in
      the same ring has two sigma bonds and an in-plane lone pair, and *is* basic — imidazole's two
      nitrogens are one of each.

    Only a **single** bond from the nitrogen counts for the amide rule, which is what keeps aniline
    out of it: aniline's bond to the ring is aromatic, not the C=O single bond this looks for, and
    aniline is genuinely a weak base (pKaH 4.6) the calibration covers.

    **Known limit.** An amide-like nitrogen *inside* an aromatic ring — caffeine's N1/N3 — is caught
    by the pyrrole-type rule (three sigma bonds) rather than the amide one, because RDKit gives its
    bonds aromatic rather than single order. Same answer by a different route.
    """
    if any(bond.GetBondType() == Chem.BondType.TRIPLE for bond in atom.GetBonds()):
        return False
    if (
        atom.GetIsAromatic()
        and atom.GetDegree() + atom.GetTotalNumHs() >= _PYRROLE_TYPE_SIGMA_BONDS
    ):
        return False
    for bond in atom.GetBonds():
        if bond.GetBondType() != Chem.BondType.SINGLE:
            continue
        neighbor = bond.GetOtherAtom(atom)
        if neighbor.GetAtomicNum() not in _ELECTRON_WITHDRAWING:
            continue
        if any(
            other.GetBondType() == Chem.BondType.DOUBLE
            and other.GetOtherAtom(neighbor).GetAtomicNum() in _CHALCOGEN
            for other in neighbor.GetBonds()
        ):
            return False
    return True


def _basic_nitrogens(mol: Chem.Mol) -> list[int]:
    """Indices of nitrogens that can be protonated: free valence *and* an available lone pair.

    The valence test alone was the whole rule until it was measured against what the base branch
    then did with the result. It counts an amide nitrogen — paracetamol's, acetamide's — and
    `predict_pka` would go on to report a basic pKa for a molecule whose only nitrogen is not basic.
    """
    return [
        atom.GetIdx()
        for atom in mol.GetAtoms()
        if atom.GetAtomicNum() == 7
        and atom.GetFormalCharge() == 0
        and atom.GetTotalNumHs() + atom.GetDegree() < _SATURATED_NITROGEN
        and _lone_pair_is_available(atom)
    ]


class IonisableSites(NamedTuple):
    """How many acid and base sites this predictor's own enumeration finds in a molecule.

    `predict_pka` reports **one** pKa — the most acidic proton, or the most stable protomer —
    because that is the number a chemist means by "the pKa". Downstream arithmetic that assumes a
    single acid/base equilibrium (`logd`'s Henderson-Hasselbalch term is the case in hand) needs to
    know when that assumption is false, and it cannot read that off a `PkaResult`: a diprotic acid
    and a monoprotic one return the same shape.
    """

    acidic: int
    basic: int

    @property
    def total(self) -> int:
        """Sites of either kind — the number a single-equilibrium model needs to be 1."""
        return self.acidic + self.basic


def ionisable_sites(smiles: str) -> IonisableSites:
    """Count the acidic O-H/S-H protons and the protonatable nitrogens of a neutral molecule.

    Structural, not energetic: it reports what `predict_pka` would *enumerate*, before any xTB runs,
    so it is free to call. It is therefore exactly as good as that enumeration and no better —
    `_basic_nitrogens` excludes amide and nitrile nitrogen because they are not basic in water, but
    it does not rank the sites it keeps.
    """
    mol = parse_molecule(smiles)
    return IonisableSites(acidic=len(_acidic_protons(mol)), basic=len(_basic_nitrogens(mol)))


def _is_aryl_nitrogen(atom: Chem.Atom) -> bool:
    """Whether a nitrogen is aromatic or attached to an aromatic system.

    The class boundary the calibration is fitted on, and it is a real one rather than a convenience:
    aryl and aromatic nitrogen delocalize into the ring, so their basicity is dominated by that
    electronic effect — which GFN2 with a continuum captures well. An aliphatic amine's aqueous
    basicity is dominated by how its ammonium ion hydrogen bonds to water, which the same model
    cannot see at all.
    """
    return atom.GetIsAromatic() or any(n.GetIsAromatic() for n in atom.GetNeighbors())


def _protonated_forms(mol: Chem.Mol, sites: list[int]) -> list[tuple[Chem.Mol, bool]]:
    """Build one cation per basic nitrogen, paired with whether that site is aryl.

    Returns `(cation, is_aryl)` so the caller can both pick the most stable protomer — the one that
    defines the conjugate acid — and know which calibration that site is in.
    """
    forms: list[tuple[Chem.Mol, bool]] = []
    for index in sites:
        editable = Chem.RWMol(mol)
        nitrogen = editable.GetAtomWithIdx(index)
        aryl = _is_aryl_nitrogen(nitrogen)
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

    One function rather than two `OptSpec(...)` literals because the second caller is
    `pka_cache_key`: the spec has to *be* the one that runs for the key to be honest about what ran,
    and two constructions of "the same" spec is how they come to differ.
    """
    return OptSpec(solvent=settings.pka_solvent)


def _relaxed_energy(mol: Chem.Mol, charge: int) -> float:
    """Solvated energy of `mol` at a GFN2-optimized geometry.

    The base path optimizes where the acid path stops at a force-field geometry, and the difference
    was measured rather than assumed: on the same seven references, MMFF geometries give Spearman
    0.893 and GFN2-optimized ones give **1.000**. Protonation changes a nitrogen's geometry
    substantially — pyramidalization, ring puckering — so the relaxation is doing real work rather
    than polishing.

    The acid calibration keeps its own force-field policy because it was fitted through that path
    and validated there; refitting it on optimized geometries is a separate, deliberate change.
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

    Raises for an aliphatic amine rather than returning a number. That is not caution — it is what
    the measurement requires: over 13 aliphatic amines the computed energy correlates with the
    experimental pKa at Spearman **-0.17**, so a prediction would carry no information while looking
    exactly like one that did.
    """
    forms = _protonated_forms(base, sites)
    if not forms:
        raise CalculationDomainError(f"no protonatable nitrogen in {smiles!r}")
    energy_base = _relaxed_energy(base, charge=0)
    # The conjugate acid is the *most stable* protomer, so it is the lowest energy that defines the
    # equilibrium — and its site decides which calibration applies.
    energy_cation, aryl = min(
        ((_relaxed_energy(cation, charge=1), aryl) for cation, aryl in forms),
        key=lambda pair: pair[0],
    )
    if not aryl:
        raise CalculationDomainError(
            f"{smiles!r} protonates on an aliphatic nitrogen, which this predictor does "
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

    Mirrors `_predict_base_pka`'s shape on the acid side: acid and anions share one geometry policy
    (MMFF where parametrized, else the embedded geometry), because the calibration was fitted
    through this exact code path, so any systematic geometry effect is absorbed into
    slope/intercept. The most acidic site is the one whose anion is most stable (lowest energy).
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

    **Canonicalizes first**, because atom order steers the seeded embedding: computing on the raw
    spelling would make the value depend on which spelling arrived first, and would put a different
    string into `calc_key`'s `input_hash` than the one the computation ran on. In Chemclaw3 that
    step lived in `run_cached_pka`; it is inside this function here so the uncached entry point
    cannot be taken wrongly.

    Raises `ValueError` on an unparseable SMILES, a net-charged or open-shell input, or a molecule
    with neither an acidic O-H/S-H site nor a basic nitrogen, rather than inventing a value. Charged
    acids are outside the calibration domain (fitted on neutral acids at charge 0 with -1 anions);
    computing them here would silently run both species at wrong electron counts and can even invert
    real acidity orderings.
    """
    canonical = require_canonical_smiles(job.smiles)
    version = calc_version()
    key = pka_cache_key(PkaInput(smiles=canonical)).as_str()

    neutral = parse_molecule(canonical)
    formal_charge = Chem.GetFormalCharge(neutral)
    if formal_charge != 0:
        raise CalculationDomainError(
            f"pKa requires a neutral acid; {job.smiles!r} has net formal charge {formal_charge}"
        )
    require_closed_shell(neutral, 0)
    anions = _conjugate_bases(neutral)
    if anions:
        return _predict_acid_pka(canonical, neutral, anions, version, key)

    # No proton to lose — but it may have a lone pair to gain one on, which is the question a
    # chemist asks about an amine. Acid first when both are present: a molecule with an O-H has a
    # pKa in the ordinary sense, and that is the number meant by "the pKa" of, say, an aminophenol.
    basic = _basic_nitrogens(neutral)
    if basic:
        return _predict_base_pka(canonical, neutral, basic, version, key)
    raise CalculationDomainError(
        f"no acidic O-H/S-H site and no basic nitrogen in {job.smiles!r}: nothing to "
        "protonate or deprotonate"
    )


def calc_version() -> str:
    """The version this calculator's results are keyed and **calibrated** under.

    Ties pKa results to method, engine build, solvent, both calibrations and both uncertainties. The
    engine build is included so a tblite or RDKit upgrade recomputes, exactly as the xTB energy
    version does. The reported `uncertainty` is part of the result, so it is versioned too —
    otherwise re-tuning `pka_uncertainty` would serve the old value.

    **The relaxation's own version is folded in**, obeying `XtbSpec.calc_version`'s rule — name
    every program whose output survives into the payload. The base branch relaxes both species
    through `optimize_structure`, which runs on `OptSpec.engine`. Unconditional, not only on the
    base branch: which branch runs is decided by the molecule *after* the version is built, and a
    version that has to re-derive the dispatch is one that can disagree with it.

    **Two costs of widening this string are not cache misses, and both are accepted deliberately.**

    A cache miss costs CPU; the *calibration ledger* costs bench work. Chemclaw3's `predictions` is
    keyed `(calc_type, calc_version, input_hash)` and read with an exact `calc_version` predicate,
    so every reconciled pKa residual recorded under a previous version becomes unreachable the
    moment this string moves: `calculator_trust("pka")` reports `UNCALIBRATED`, n=0, until each
    molecule is predicted again.

    And under the default `xtb_engine=auto`, `relaxation_spec()` resolves a concrete backend, so a
    pod **with** the `xtb` binary and one **without** compute different pKa versions. That is
    wanted: the base branch really does relax through whichever backend is present, the two do not
    agree to the last decimal, and a shared version would serve one program's number as the other's.
    Pinning `CHEMCLAW_XTB_ENGINE` removes the split for a deployment that would rather not pay it.

    **After the split, this is a string only this server can produce.** Neither `tblite` nor `rdkit`
    is installed on a Chemclaw3 pod, and `xtb_cli.binary_version()` answers `"absent"` rather than
    raising — so a client deriving it locally gets a well-formed string matching nothing.
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

    Expects a job whose SMILES is already canonical (`predict_pka` canonicalizes before calling):
    atom order steers the seeded embedding, so a key built from one spelling and a computation run
    on another would name a value that depends on which arrived first.

    The relaxation's *knobs* land in `params` for the same reason its programs land in
    `calc_version` — `XtbSpec.cache_key` splits them exactly this way. Measured, they move the
    answer: pyridine comes out at 5.400052 / 5.402952 / 5.335181 for gradient tolerances 5e-4 / 5e-3
    / 2e-2, and before this all three were one key.
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
