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
and the solvent — plus `engine_version()` (the tblite, RDKit and scipy distributions) plus the
relaxation's own `OptSpec.calc_version()`, which resolves the backend, names the geomeTRIC
distribution where that backend is the in-process one, and may shell out to `xtb --version` where
it is not.
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
# **This is a transcription of the imperative rules that stood here, not a redesign of them**, and
# the distinction is the whole reason the table is worth reading carefully. `calc_version()`'s
# linear calibration was fitted over *this* enumeration: a broader site set would pick a different
# most-stable protomer on some molecules, and the calibration ledger on Chemclaw3's side matches
# `calc_version` exactly with no version pooling — so a site rule that is "better" and different is
# a silent recalibration of every stored residual. Proven equal to the fifty lines of `GetBonds()`
# walking it replaced over a **216-molecule** probe corpus (the `props` solvent table, the `chem`
# reagent table, and ~100 hand-written cases chosen to hit every arm: thioamides, phosphoramides,
# N-oxides, azo and imine nitrogen, quaternary ammonium, isocyanates, hydrazides, peroxides,
# fused azoles): **zero** disagreements on the acidic set, the basic set and the aryl
# classification.
#
# `dimorphite-dl` was considered for this and declined for the same reason: it perceives a broader
# set, which is exactly what must not change.
#
# **"Proven equal" was proven below a ceiling nobody mentioned, and `_all_matches` is where that
# ceiling is now lifted** (`D-2026-09-16-a-default-ceiling-is-a-silent-truncation`).
# `Mol.GetSubstructMatches` stops at **1,000** matches by default and says nothing; the bond walking
# it replaced had no bound at all. Measured on `"N" * 1500` — a nitrogen chain of 1,500 heavy atoms,
# inside `mcp_server_kit.limits.MAX_MOLECULE_ATOMS` and `MAX_SMILES_CHARS` — `_basic_nitrogens`
# returned 1,000 where the walk counts 1,500, and `ionisable_sites` reported `basic=1000`. The
# 216-molecule agreement stands: every probe is far below the ceiling, which is exactly why the
# ceiling could not appear in it.
# ---------------------------------------------------------------------------------------------

#: Every O-H/S-H proton, on an explicit-hydrogen molecule (`parse_molecule`'s output). The match is
#: `(hydrogen, heavy atom)` in that order, which is the pair `_conjugate_bases` deprotonates.
#: `X1` on the hydrogen is the old `GetDegree() == 1`; it excludes nothing real and is kept because
#: a bridging hydride would otherwise read as an acidic proton.
_ACIDIC_PROTON = Chem.MolFromSmarts("[#1X1][#8,#16]")

#: A nitrogen that can actually accept a proton in water. Neutral, with a free valence, minus the
#: three classes whose lone pair is not available — each of which was a paragraph of bond walking
#: and is now one recursive SMARTS:
#:
#: - `!$([#7]#*)` — **nitrile** (and any other sp nitrogen). pKaH ~ -10; there is no aqueous pH at
#:   which any of it is protonated.
#: - `!$([nX3])` — **pyrrole-type aromatic nitrogen**: three connections on an aromatic nitrogen, so
#:   its lone pair is the ring's aromatic sextet rather than an in-plane orbital. Pyrrole's pKaH is
#:   ~ -4 and protonating it costs the ring its aromaticity. The **pyridine-type** nitrogen beside
#: it
#:   has two connections and *is* basic — imidazole's two nitrogens are one of each. On an
#:   explicit-hydrogen molecule `X` is the old `GetDegree() + GetTotalNumHs()`.
#: - `!$([#7]-[#6,#16]=[#8,#16])` — **amide, carbamate, urea, sulfonamide, sulfinamide, and their
#:   thio analogues**: a nitrogen *singly* bonded to a carbon or sulfur that carries a double bond
#: to
#:   O or S. The lone pair is conjugated into that C=O/S=O, and the consequence is not a shifted pKa
#:   but a different molecule — protonated acetamide has pKaH ~ -0.5 **and protonates on the
#:   oxygen**, so the nitrogen this enumeration would otherwise offer is not the site even in the
#:   strongest acid. The explicit single bond is what keeps aniline out of it: aniline's bond to the
#:   ring is aromatic, and aniline is a genuine weak base (pKaH 4.6) the calibration covers.
#:
#: **Known limit, unchanged by the transcription.** An amide-like nitrogen *inside* an aromatic ring
#: — caffeine's N1/N3 — is caught by the pyrrole-type arm rather than the amide one, because RDKit
#: gives its bonds aromatic rather than single order. Same answer by a different route.
#:
#: **What it does not exclude, also unchanged.** A nitrogen on phosphorus (`N-P=O`, a phosphoramide)
#: is not in the electron-withdrawing arm, which names carbon and sulfur only. That was true of the
#: bond-walking version and is true here; the probe corpus contains the case so that a future
#: widening
#: is a visible diff rather than a silent one.
_BASIC_NITROGEN = Chem.MolFromSmarts(
    "[#7+0;X1,X2,X3;!$([#7]#*);!$([nX3]);!$([#7]-[#6,#16]=[#8,#16])]"
)

#: Aromatic, or attached to an aromatic system — the boundary the *base* calibration is fitted on,
#: and a real one rather than a convenience: aryl and aromatic nitrogen delocalize into the ring, so
#: their basicity is dominated by that electronic effect, which GFN2 with a continuum captures well
#: (Spearman 1.000 over seven references). An aliphatic amine's aqueous basicity is set by how its
#: ammonium ion hydrogen bonds to water, which the same model cannot see at all (Spearman -0.17),
#: and is refused rather than reported.
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
    # "acid" = an O-H/S-H proton came off; "base" = the pKa of the protonated form (pKaH), which is
    # what is tabulated for amines and what an extraction pH is set against. Each has its own
    # calibration, fitted separately.
    site: Literal["acid", "base"] = "acid"


def _all_matches(mol: Chem.Mol, pattern: Chem.Mol) -> list[tuple[int, ...]]:
    """Every match of `pattern` in `mol`, with RDKit's silent 1,000-match ceiling lifted.

    `GetSubstructMatches` defaults to `maxMatches=1000` and truncates quietly — no warning, no
    flag on the result, a short list that looks like a complete one. Every pattern here is anchored
    on a distinct atom (a hydrogen for `_ACIDIC_PROTON`, a nitrogen for the other two), so the
    number of matches cannot exceed the atom count, and the atom count is therefore a bound that
    can be *derived* rather than chosen. A configured number would be a second ceiling to keep in
    step with `mcp_server_kit.limits.MAX_MOLECULE_ATOMS`; this one moves with the molecule.

    `max(..., 1)` because `maxMatches=0` is not "no limit" in RDKit's API and an empty molecule
    should not take a different code path from a full one.
    """
    matches: list[tuple[int, ...]] = list(
        mol.GetSubstructMatches(pattern, maxMatches=max(mol.GetNumAtoms(), 1))
    )
    return matches


def _acidic_protons(mol: Chem.Mol) -> list[tuple[int, int]]:
    """`(hydrogen index, heavy-atom index)` for every O-H/S-H proton, explicit-H molecule.

    The module's one definition of "acidic site": `_conjugate_bases` deprotonates exactly these and
    `ionisable_sites` counts exactly these, so a caller asking *how many* sites a molecule has
    cannot disagree with the enumeration that produced the pKa.

    Sorted by hydrogen index, which is the order the atom walk this replaced produced. It decides
    nothing about the answer — the most stable anion wins on energy — but it decides which of two
    exactly degenerate sites is reported, and a reordering there would be a diff in a stored result
    with no physics behind it.
    """
    # `(match[0], match[1])` rather than `match`: `_all_matches` is shared with the two
    # single-atom patterns, so its element type is the widest of the three.
    return sorted((match[0], match[1]) for match in _all_matches(mol, _ACIDIC_PROTON))


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


def _basic_nitrogens(mol: Chem.Mol) -> list[int]:
    """Indices of nitrogens that can be protonated — free valence *and* an available lone pair.

    The valence test alone was the whole rule until it was measured against what the base branch
    then did with the result. It counts an amide nitrogen — paracetamol's, acetamide's — and
    `predict_pka` would go on to report a basic pKa for a molecule whose only nitrogen is not basic.
    Both halves are now one pattern; `_BASIC_NITROGEN` is where the argument for each exclusion is.
    """
    return sorted(match[0] for match in _all_matches(mol, _BASIC_NITROGEN))


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


def _aryl_nitrogens(mol: Chem.Mol) -> set[int]:
    """Indices of the nitrogens `_ARYL_NITROGEN` matches — the class the base calibration covers.

    Computed once per molecule and passed down rather than asked per atom, because a recursive
    SMARTS is matched against the whole molecule either way and `_protonated_forms` would otherwise
    re-run it per site.
    """
    return {match[0] for match in _all_matches(mol, _ARYL_NITROGEN)}


def _protonated_forms(mol: Chem.Mol, sites: list[int]) -> list[tuple[Chem.Mol, bool]]:
    """Build one cation per basic nitrogen, paired with whether that site is aryl.

    Returns `(cation, is_aryl)` so the caller can both pick the most stable protomer — the one that
    defines the conjugate acid — and know which calibration that site is in.
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
            f"pKa requires a neutral acid; {echo(job.smiles)!r} has net formal charge "
            f"{formal_charge}"
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
        f"no acidic O-H/S-H site and no basic nitrogen in {echo(job.smiles)!r}: nothing to "
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
