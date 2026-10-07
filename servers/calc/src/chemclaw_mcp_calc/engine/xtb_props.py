"""Electronic properties and site reactivity from GFN2-xTB.

- `compute_properties`: frontier orbitals, dipole, Mulliken charges and Wiberg bond orders, all
  read from the energy's own single point.
- `compute_fukui`: condensed Fukui indices by finite difference over three single points on one
  geometry (N, N-1, N+1 electrons), plus the conceptual-DFT global panel and local descriptors,
  which are arithmetic on the same three energies — no fourth SCF.

Fukui indices need a closed-shell parent, and rank sites *within* one molecule only. `mode` only
chooses the sort, so it is outside the key and `ranked_for` re-ranks without recomputing.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
from pydantic import BaseModel, Field
from rdkit import Chem
from scipy import constants

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
    "GlobalDescriptors",
    "PropertiesSpec",
    "SiteReactivityResult",
    "compute_fukui",
    "compute_properties",
    "fukui_inputs",
    "properties_inputs",
    "property_structure",
    "ranked_for",
]

# Hartree to eV, derived from `scipy.constants` like `xtb_engine`'s conversions (whose Debye factor
# is imported); the CODATA edition is in `engine_version()`.
_HARTREE_TO_EV = constants.value("Hartree energy in eV")

# Occupation above which an orbital counts as occupied; a gapped molecule's occupations are 2 and 0.
_OCCUPIED = 0.5

# Which attack a Fukui function describes: f-minus for an electrophile, f-plus for a nucleophile,
# f-zero (their average) for radicals.
FukuiMode = Literal["electrophilic", "nucleophilic", "radical"]
_MODE_FIELD: dict[FukuiMode, str] = {
    "electrophilic": "f_minus",
    "nucleophilic": "f_plus",
    "radical": "f_zero",
}


class PropertiesSpec(XtbSpec):
    """Settings of one electronic-properties calculation.

    The bond-order threshold filters the payload, so it must be in the key: an argument outside the
    key may permute the answer but never remove from it, and a bond dropped from a cached row would
    be missing from Chemclaw3's published record permanently. A subclass field so a single point's
    key does not carry it; keyed by construction via `model_dump()`.
    """

    task: Literal["properties"] = "properties"
    # Wiberg bond order at or above which a pair of atoms is reported as bonded. `ge=0` rather than
    # `gt=0`: zero is the meaningful "report every pair" end of the range, not a degenerate value.
    bond_order_threshold: float = Field(
        default_factory=lambda: settings.xtb_bond_order_threshold, ge=0
    )


class AtomCharge(BaseModel):
    """One atom's Mulliken partial charge and its Wiberg valence, with the index to locate it.

    `free_valence` is the classical Coulson radical index — the atom's normal valence minus the
    bond order it actually uses — so a large value marks an atom with bonding capacity to spare.
    It is `None` for an element whose valence RDKit reports as variable (hypervalent sulfur and
    phosphorus), because subtracting from a number nobody can state is not a descriptor.
    """

    index: int
    element: str
    charge: float
    wiberg_valence: float = Field(
        description="Sum of this atom's Wiberg bond orders to every other atom."
    )
    free_valence: float | None = Field(
        default=None,
        description="Normal valence minus `wiberg_valence`, or null where the "
        "element has no single normal valence.",
    )


class BondOrder(BaseModel):
    """A Wiberg bond order between two atoms, above the reporting threshold."""

    atom_i: int
    atom_j: int
    order: float


class ElectronicProperties(Keyed):
    """The electronic structure of one geometry, as read from a single GFN2-xTB SCF.

    `homo_ev`/`lumo_ev`/`gap_ev` are orbital energies, not ionization potentials — useful for
    comparing related molecules. `lumo_ev` and `gap_ev` are None with no virtual orbital.
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


class GlobalDescriptors(BaseModel):
    """Conceptual-DFT global reactivity descriptors for one molecule, in eV.

    Derived from **vertical Delta-SCF** energies rather than from Koopmans' theorem: the three
    single points the Fukui path runs already give E(N), E(N-1) and E(N+1) on one fixed geometry, so
    `IP = E(N-1) - E(N)` and `EA = E(N) - E(N+1)` are differences between numbers already computed.
    That is strictly better than reading the frontier orbital energies, and it is free.

    **These rank a series; they do not measure an ionization potential.** Measured against GFN2,
    phenol comes out at 13.5 eV against an experimental 8.5 — the semiempirical Hamiltonian is not
    parameterised for absolute ionization energetics, and no amount of arithmetic downstream fixes
    that. Use them to order related molecules, register them with the calibration ledger, and never
    quote one as a measurement.
    """

    ionization_potential_ev: float = Field(
        description="Vertical Delta-SCF IP: E(N-1) - E(N). Uncalibrated in absolute terms."
    )
    electron_affinity_ev: float = Field(description="Vertical Delta-SCF EA: E(N) - E(N+1).")
    chemical_potential_ev: float = Field(
        description="mu = -(IP + EA)/2 — the escaping tendency of the electrons. Negative."
    )
    hardness_ev: float = Field(
        description="eta = IP - EA. Resistance to charge transfer; large means hard."
    )
    softness_per_ev: float = Field(description="S = 1/eta, in 1/eV. What local softness scales.")
    electrophilicity_ev: float = Field(
        description="omega = mu^2 / (2 * eta) — the energy stabilisation on saturating with "
        "electrons. The global scale behind `local_electrophilicity_ev`."
    )


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
    dual: float = Field(
        description=(
            "f_plus - f_minus. Positive marks a site that accepts electrons more readily than it "
            "donates (electrophilic in character), negative the reverse. One number where the two "
            "Fukui indices are two, which is what makes a cycloaddition's large-with-large pairing "
            "rule sayable."
        )
    )
    local_softness_minus: float = Field(
        description="S * f_minus, in 1/eV. Softness partitioned onto this site."
    )
    local_softness_plus: float = Field(description="S * f_plus, in 1/eV.")
    local_electrophilicity_ev: float = Field(
        description=(
            "omega * f_plus, in eV — the global electrophilicity partitioned onto this site. The "
            "one quantity here carrying a global scale factor, so it is the only one with any "
            "chance of ranking sites *across* molecules; whether it actually does is a calibration "
            "question, not a settled one."
        )
    )


class SiteReactivityResult(Keyed):
    """Atoms ranked by susceptibility to the requested attack.

    `sites` is ordered most-susceptible first by `ranked_by` and always holds one entry per atom, so
    a cached row (keyed without `mode`) can answer every mode via `ranked_for`; shortlisting is the
    caller's job. Valid within this molecule only, and electronic susceptibility alone (no sterics,
    no specific reagent).
    """

    smiles: str | None
    structure_id: str
    method: str
    solvent: str | None
    mode: FukuiMode
    ranked_by: str
    total_atoms: int
    descriptors: GlobalDescriptors
    sites: list[FukuiSite]


def _frontier_orbitals(energies: np.ndarray, occupations: np.ndarray) -> tuple[float, float | None]:
    """Return (HOMO, LUMO) energies in Hartree; LUMO is None with no virtual orbital.

    Read from occupations, not an electron count, so it holds for the open-shell Fukui ions.
    """
    occupied = np.flatnonzero(occupations > _OCCUPIED)
    if occupied.size == 0:
        raise ValueError("no occupied orbitals: not a valid electronic structure")
    homo_index = int(occupied[-1])
    lumo = float(energies[homo_index + 1]) if homo_index + 1 < energies.size else None
    return float(energies[homo_index]), lumo


def _wiberg(matrix: np.ndarray) -> np.ndarray:
    """The Wiberg bond-order matrix, with tblite's trailing spin dimension dropped.

    One channel, taken in one place, since no spin-polarised property calculation runs here.
    """
    return np.asarray(matrix)[:, :, 0]


def _valences(
    wiberg: np.ndarray, symbols: list[str], charges: list[int]
) -> list[tuple[float, float | None]]:
    """Each atom's total Wiberg valence and its free valence.

    Free valence is Coulson's radical index: normal valence minus the bond order used. `None` where
    the element has no single normal valence (RDKit's valence list has several, e.g. sulfur), since
    subtracting from a default would give meaningless negative values.
    """
    table = Chem.GetPeriodicTable()
    used = wiberg.sum(axis=1)
    valences: list[tuple[float, float | None]] = []
    for symbol, charge, total in zip(symbols, charges, used, strict=True):
        allowed = list(table.GetValenceList(symbol))
        # No single normal valence: several allowed valences, or a formal charge.
        undefined = len(allowed) != 1 or charge != 0
        free = None if undefined else round(allowed[0] - float(total), 3)
        valences.append((round(float(total), 3), free))
    return valences


def _formal_charges(structure: Structure) -> list[int]:
    """Per-atom formal charges for `structure`, read back from the canonical SMILES it carries.

    `Structure` records only the molecular charge. `AddHs` over the stored canonical SMILES
    reproduces the structure's atom order. With no SMILES, returns a non-zero sentinel for every
    atom so no free valence is computed against an assumed neutrality.
    """
    if structure.smiles is None:
        return [1] * len(structure.elements)
    molecule = Chem.AddHs(Chem.MolFromSmiles(structure.smiles))
    charges = [atom.GetFormalCharge() for atom in molecule.GetAtoms()]
    # A mismatch means the SMILES does not describe this geometry; refuse the descriptor rather
    # than pair charges with the wrong atoms.
    return charges if len(charges) == len(structure.elements) else [1] * len(structure.elements)


def _bond_orders(matrix: np.ndarray, threshold: float) -> list[BondOrder]:
    """Upper-triangle bond orders at or above `threshold`, strongest first."""
    wiberg = _wiberg(matrix)
    pairs = [
        BondOrder(atom_i=int(i), atom_j=int(j), order=round(float(wiberg[i, j]), 3))
        for i, j in zip(*np.triu_indices_from(wiberg, k=1), strict=True)
        if wiberg[i, j] >= threshold
    ]
    return sorted(pairs, key=lambda bond: bond.order, reverse=True)


def compute_properties(spec: PropertiesSpec, structure: Structure) -> ElectronicProperties:
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
    valences = _valences(_wiberg(result["bond-orders"]), symbols, _formal_charges(structure))
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
            AtomCharge(
                index=index,
                element=symbol,
                charge=round(float(charge), 4),
                wiberg_valence=valence,
                free_valence=free,
            )
            for index, (symbol, charge, (valence, free)) in enumerate(
                zip(symbols, result["charges"], valences, strict=True)
            )
        ],
        bond_orders=_bond_orders(result["bond-orders"], resolved.bond_order_threshold),
    )


def _global_descriptors(neutral: float, cation: float, anion: float) -> GlobalDescriptors:
    """The conceptual-DFT panel from three total energies in Hartree.

    Raises `ValueError` on a non-positive hardness (`eta = IP - EA`, a divisor): the three SCFs then
    do not describe one consistent system.
    """
    ionization = (cation - neutral) * _HARTREE_TO_EV
    affinity = (neutral - anion) * _HARTREE_TO_EV
    hardness = ionization - affinity
    if hardness <= 0:
        raise ValueError(
            "non-positive chemical hardness "
            f"(IP {ionization:.3f} eV, EA {affinity:.3f} eV): the neutral, cation and anion single "
            "points do not describe one consistent electronic system, so no global descriptor "
            "derived from them would mean anything"
        )
    potential = -(ionization + affinity) / 2
    return GlobalDescriptors(
        ionization_potential_ev=round(ionization, 4),
        electron_affinity_ev=round(affinity, 4),
        chemical_potential_ev=round(potential, 4),
        hardness_ev=round(hardness, 4),
        softness_per_ev=round(1 / hardness, 6),
        electrophilicity_ev=round(potential**2 / (2 * hardness), 4),
    )


def _site(
    index: int, element: str, minus: float, plus: float, panel: GlobalDescriptors
) -> FukuiSite:
    """One atom's Fukui indices and everything derived from them.

    Derived values use the *rounded* f-minus and f-plus, so a caller recomputing them from the
    reported numbers gets the reported numbers.
    """
    f_minus = round(minus, 4)
    f_plus = round(plus, 4)
    return FukuiSite(
        index=index,
        element=element,
        f_minus=f_minus,
        f_plus=f_plus,
        f_zero=round((f_minus + f_plus) / 2, 4),
        dual=round(f_plus - f_minus, 4),
        local_softness_minus=round(panel.softness_per_ev * f_minus, 6),
        local_softness_plus=round(panel.softness_per_ev * f_plus, 6),
        local_electrophilicity_ev=round(panel.electrophilicity_ev * f_plus, 4),
    )


def compute_fukui(spec: XtbSpec, structure: Structure, mode: FukuiMode) -> SiteReactivityResult:
    """Rank the atoms of `structure` by their condensed Fukui index for `mode`.

    Three single points on one geometry; in terms of Mulliken charges q (population is Z - q):

        f-(k) = q(k, N-1) - q(k, N)     electrophilic attack
        f+(k) = q(k, N)   - q(k, N+1)   nucleophilic attack
        f0(k) = (f- + f+) / 2           radical attack

    `IP = E(N-1) - E(N)` and `EA = E(N) - E(N+1)` (vertical, fixed geometry) give the global panel
    and the local descriptors from the same three results.

    Raises `ValueError` for an open-shell parent, whose ions' spin states would be a guess.
    """
    if structure.uhf:
        raise ValueError(
            "Fukui indices require a closed-shell molecule; "
            f"this structure has {structure.uhf} unpaired electron(s)"
        )
    resolved = spec.for_structure(structure)
    numbers, positions = structure.arrays()

    def single_point(charge: int, uhf: int) -> tuple[np.ndarray, float]:
        """The Mulliken charges *and* the total energy of one ionization state."""
        result = run_singlepoint(
            resolved.method,
            numbers,
            positions,
            charge=charge,
            uhf=uhf,
            solvent=resolved.solvent,
        )
        return np.asarray(result["charges"]), float(result["energy"])

    # Removing or adding one electron from a closed shell leaves exactly one unpaired.
    neutral, neutral_energy = single_point(structure.charge, 0)
    cation, cation_energy = single_point(structure.charge + 1, 1)
    anion, anion_energy = single_point(structure.charge - 1, 1)

    descriptors = _global_descriptors(neutral_energy, cation_energy, anion_energy)
    f_minus = cation - neutral
    f_plus = neutral - anion
    symbols = structure.symbols
    sites = [
        _site(index, symbol, float(minus), float(plus), descriptors)
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
        descriptors=descriptors,
        sites=sites,
    )


def property_structure(smiles: str) -> Structure:
    """Embed `smiles` under the geometry policy both property tasks share.

    MMFF pre-optimization is required: a raw ETKDG embedding breaks the symmetry of equivalent ring
    positions and can invert Fukui orderings. Results describe a force-field geometry, which
    `structure_id` records.
    """
    return structure_from_smiles(smiles, optimize=True)


def properties_inputs(smiles: str, solvent: str | None = None) -> tuple[PropertiesSpec, Structure]:
    """The settings and the geometry `compute_properties` runs on — see `xtb.sp_inputs` for why.

    The solvent is validated at spec construction, before embedding, so `calculation_identity`
    refuses it identically.
    """
    return PropertiesSpec(solvent=solvent), property_structure(smiles)


def fukui_inputs(smiles: str, solvent: str | None = None) -> tuple[XtbSpec, Structure]:
    """The settings and the geometry `compute_fukui` runs on.

    `mode` is absent: it only chooses the sort, so a second ranking needs no second calculation.
    """
    return XtbSpec(task="fukui", solvent=solvent), property_structure(smiles)


def ranked_for(result: SiteReactivityResult, mode: FukuiMode) -> SiteReactivityResult:
    """Re-rank a Fukui result for `mode` without recomputing anything."""
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
