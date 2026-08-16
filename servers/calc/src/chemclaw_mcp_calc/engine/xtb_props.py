"""Electronic properties and site reactivity from GFN2-xTB.

Two calculators over the SCF the energy calculator already runs:

- `compute_properties` — frontier orbitals, dipole, Mulliken charges and Wiberg bond orders. These
  come out of the *same* single point as the energy, so the whole capability costs nothing beyond
  reading the result we were already discarding.
- `compute_fukui` — condensed Fukui indices from the finite-difference definition, three single
  points on one fixed geometry (N, N-1 and N+1 electrons). This is what answers "which atom reacts".

Both are honest about their domain rather than clever about it: Fukui indices need a closed-shell
parent (the ions' doublet state is then unambiguous), and the ranking they produce compares sites
*within* one molecule and never across molecules.

**Ported without the cache.** `run_cached_properties`/`run_cached_fukui` looked the answer up first;
these compute and return, carrying the key they would have been stored under. One behaviour of the
cached path is deliberately kept: the three Fukui single points do not depend on `mode` — it only
chooses the sort — so `compute_fukui` computes once and `ranked_for` re-ranks, which is why a caller
asking a second mode pays for a re-sort rather than three more SCFs.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
from pydantic import BaseModel, Field

from chemclaw_mcp_calc.engine.config import settings
from chemclaw_mcp_calc.engine.key import Keyed
from chemclaw_mcp_calc.engine.structure import Structure, structure_from_smiles
from chemclaw_mcp_calc.engine.xtb_engine import AU_TO_DEBYE, run_singlepoint
from chemclaw_mcp_calc.engine.xtb_spec import XtbSpec

__all__ = [
    "AtomCharge",
    "BondOrder",
    "ElectronicProperties",
    "FukuiMode",
    "FukuiSite",
    "SiteReactivityResult",
    "compute_fukui",
    "compute_properties",
    "property_structure",
    "ranked_for",
]

# Physical constants, in the direction this module converts. The Debye conversion is imported from
# the unit boundary (`xtb_engine`) rather than restated here.
_HARTREE_TO_EV = 27.211386245988

# An orbital counts as occupied above this occupation number. Fermi smearing at the default
# electronic temperature leaves a gapped molecule's occupations at 2 and 0, so the threshold only
# has to separate "occupied" from "empty", not resolve fractions.
_OCCUPIED = 0.5

# Which attack a Fukui function describes. f-minus (loss of an electron) marks the sites a deficient
# *electrophile* attacks; f-plus (gain) the sites a *nucleophile* attacks; f-zero their average, for
# radicals.
FukuiMode = Literal["electrophilic", "nucleophilic", "radical"]
_MODE_FIELD: dict[FukuiMode, str] = {
    "electrophilic": "f_minus",
    "nucleophilic": "f_plus",
    "radical": "f_zero",
}


class AtomCharge(BaseModel):
    """One atom's Mulliken partial charge, with the index a chemist can locate."""

    index: int
    element: str
    charge: float


class BondOrder(BaseModel):
    """A Wiberg bond order between two atoms, above the reporting threshold."""

    atom_i: int
    atom_j: int
    order: float


class ElectronicProperties(Keyed):
    """The electronic structure of one geometry, as read from a single GFN2-xTB SCF.

    `homo_ev`/`lumo_ev`/`gap_ev` are frontier orbital energies, not ionization potentials —
    semiempirical orbital energies are useful for *comparing* related molecules and poor as absolute
    quantities. `lumo_ev` and `gap_ev` are None for the rare system with no virtual orbital.
    """

    smiles: str | None
    structure_id: str
    method: str
    solvent: str | None
    total_energy_hartree: float
    homo_ev: float
    lumo_ev: float | None
    gap_ev: float | None
    dipole_debye: float
    atom_charges: list[AtomCharge]
    bond_orders: list[BondOrder]


class FukuiSite(BaseModel):
    """Condensed Fukui indices for one atom.

    By construction `f_zero` is the mean of `f_minus` and `f_plus`. A larger value means the site is
    more susceptible to the corresponding attack.
    """

    index: int
    element: str
    f_minus: float = Field(description="electrophilic attack (site donates electrons)")
    f_plus: float = Field(description="nucleophilic attack (site accepts electrons)")
    f_zero: float = Field(description="radical attack (the mean of the other two)")


class SiteReactivityResult(Keyed):
    """Atoms ranked by susceptibility to the requested attack.

    `sites` is ordered most-susceptible first by the index named in `ranked_by`, and truncated to
    the most susceptible `len(sites)` of `total_atoms`. The ranking is valid *within* this molecule
    only: Fukui indices are normalized per molecule, so comparing them between molecules is
    meaningless, and they describe electronic susceptibility alone — sterics and the specific
    reagent are not in the model.
    """

    smiles: str | None
    structure_id: str
    method: str
    solvent: str | None
    mode: FukuiMode
    ranked_by: str
    total_atoms: int
    sites: list[FukuiSite]


def _frontier_orbitals(energies: np.ndarray, occupations: np.ndarray) -> tuple[float, float | None]:
    """Return (HOMO, LUMO) energies in Hartree; LUMO is None with no virtual orbital.

    The HOMO is the highest orbital carrying electrons and the LUMO the next one up, read from the
    occupations rather than from an electron count — which keeps the definition correct for the
    open-shell ions the Fukui path computes.
    """
    occupied = np.flatnonzero(occupations > _OCCUPIED)
    if occupied.size == 0:
        raise ValueError("no occupied orbitals: not a valid electronic structure")
    homo_index = int(occupied[-1])
    lumo = float(energies[homo_index + 1]) if homo_index + 1 < energies.size else None
    return float(energies[homo_index]), lumo


def _bond_orders(matrix: np.ndarray, threshold: float) -> list[BondOrder]:
    """Upper-triangle bond orders above `threshold`, strongest first."""
    # tblite returns the Wiberg matrix with a trailing spin dimension; one channel here.
    wiberg = np.asarray(matrix)[:, :, 0]
    pairs = [
        BondOrder(atom_i=int(i), atom_j=int(j), order=round(float(wiberg[i, j]), 3))
        for i, j in zip(*np.triu_indices_from(wiberg, k=1), strict=True)
        if wiberg[i, j] >= threshold
    ]
    return sorted(pairs, key=lambda bond: bond.order, reverse=True)


def compute_properties(spec: XtbSpec, structure: Structure) -> ElectronicProperties:
    """Read the electronic properties of `structure` from one GFN2-xTB single point."""
    resolved = spec.for_structure(structure)
    numbers, positions = structure.arrays()
    result = run_singlepoint(
        resolved.method,
        numbers,
        positions,
        charge=structure.charge,
        uhf=structure.uhf,
        solvent=resolved.solvent,
    )
    homo, lumo = _frontier_orbitals(result["orbital-energies"], result["orbital-occupations"])
    symbols = structure.symbols
    return ElectronicProperties(
        calc_version=resolved.calc_version(),
        calc_key=resolved.cache_key(structure).as_str(),
        smiles=structure.smiles,
        structure_id=structure.structure_id,
        method=resolved.method,
        solvent=resolved.solvent,
        total_energy_hartree=float(result["energy"]),
        homo_ev=homo * _HARTREE_TO_EV,
        lumo_ev=None if lumo is None else lumo * _HARTREE_TO_EV,
        gap_ev=None if lumo is None else (lumo - homo) * _HARTREE_TO_EV,
        dipole_debye=float(np.linalg.norm(result["dipole"])) * AU_TO_DEBYE,
        atom_charges=[
            AtomCharge(index=index, element=symbol, charge=round(float(charge), 4))
            for index, (symbol, charge) in enumerate(zip(symbols, result["charges"], strict=True))
        ],
        bond_orders=_bond_orders(result["bond-orders"], settings.xtb_bond_order_threshold),
    )


def compute_fukui(spec: XtbSpec, structure: Structure, mode: FukuiMode) -> SiteReactivityResult:
    """Rank the atoms of `structure` by their condensed Fukui index for `mode`.

    Runs three single points on the *same* geometry — the neutral molecule and its one-electron
    oxidized and reduced forms — and takes the Mulliken charge differences. In terms of charges q
    (electron population is Z - q):

        f-(k) = q(k, N-1) - q(k, N)     electrophilic attack
        f+(k) = q(k, N)   - q(k, N+1)   nucleophilic attack
        f0(k) = (f- + f+) / 2           radical attack

    Raises `ValueError` for an open-shell parent: both ions of a closed-shell molecule are
    unambiguously doublets, while the ions of an open-shell parent could be either of two spin
    states and picking one silently would be guessing.
    """
    if structure.uhf:
        raise ValueError(
            "Fukui indices require a closed-shell molecule; "
            f"this structure has {structure.uhf} unpaired electron(s)"
        )
    resolved = spec.for_structure(structure)
    numbers, positions = structure.arrays()

    def charges(charge: int, uhf: int) -> np.ndarray:
        return np.asarray(
            run_singlepoint(
                resolved.method,
                numbers,
                positions,
                charge=charge,
                uhf=uhf,
                solvent=resolved.solvent,
            )["charges"]
        )

    # Removing or adding one electron from a closed shell leaves exactly one unpaired.
    neutral = charges(structure.charge, 0)
    cation = charges(structure.charge + 1, 1)
    anion = charges(structure.charge - 1, 1)

    f_minus = cation - neutral
    f_plus = neutral - anion
    symbols = structure.symbols
    sites = [
        FukuiSite(
            index=index,
            element=symbol,
            f_minus=round(float(minus), 4),
            f_plus=round(float(plus), 4),
            f_zero=round(float(minus + plus) / 2, 4),
        )
        for index, (symbol, minus, plus) in enumerate(zip(symbols, f_minus, f_plus, strict=True))
    ]
    ranked_by = _MODE_FIELD[mode]
    sites.sort(key=lambda site: getattr(site, ranked_by), reverse=True)
    return SiteReactivityResult(
        calc_version=resolved.calc_version(),
        calc_key=resolved.cache_key(structure).as_str(),
        smiles=structure.smiles,
        structure_id=structure.structure_id,
        method=resolved.method,
        solvent=resolved.solvent,
        mode=mode,
        ranked_by=ranked_by,
        total_atoms=len(sites),
        sites=sites,
    )


def property_structure(smiles: str) -> Structure:
    """Embed `smiles` under the geometry policy both property tasks share.

    MMFF pre-optimization is **required here, not merely nicer**, and the reason is measurable: on a
    raw ETKDG embedding the residual distortion is large enough to break the symmetry of chemically
    equivalent ring positions, which inverts the Fukui ordering of phenol and toluene (*ortho* and
    *meta* overlap). Relaxing first restores the equivalence — toluene's two *ortho* carbons agree
    to 1e-4 — and recovers the correct *para* > *ortho* > *meta* ordering. `pka` sets the same flag
    for the same reason.

    A GFN2 optimization would be better still. Until then the honest statement is that these results
    describe a force-field geometry, which is what `structure_id` records.
    """
    return structure_from_smiles(smiles, optimize=True)


def ranked_for(result: SiteReactivityResult, mode: FukuiMode) -> SiteReactivityResult:
    """Re-rank a Fukui result for `mode` without recomputing anything.

    Public here where Chemclaw3 kept it private, because the caching layer that used to call it is
    gone: a caller holding a result and wanting the other two rankings must be able to get them for
    a sort rather than for three more SCFs.
    """
    if result.mode == mode:
        return result
    ranked_by = _MODE_FIELD[mode]
    return result.model_copy(
        update={
            "mode": mode,
            "ranked_by": ranked_by,
            "sites": sorted(result.sites, key=lambda site: getattr(site, ranked_by), reverse=True),
        }
    )
