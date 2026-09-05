"""Electronic properties and site reactivity from GFN2-xTB.

Two calculators over the SCF the energy calculator already runs:

- `compute_properties` — frontier orbitals, dipole, Mulliken charges and Wiberg bond orders. These
  come out of the *same* single point as the energy, so the whole capability costs nothing beyond
  reading the result we were already discarding.
- `compute_fukui` — condensed Fukui indices from the finite-difference definition, three single
  points on one fixed geometry (N, N-1 and N+1 electrons). This is what answers "which atom reacts".
  It also returns the **conceptual-DFT global panel** and the local descriptors derived from it, and
  those cost nothing: the ionization potential and electron affinity are differences between the
  three energies this function already computes and used to discard, reading only `["charges"]` out
  of each result. Everything downstream — chemical potential, hardness, softness, electrophilicity,
  and then local softness and local electrophilicity per atom — is arithmetic on those. No fourth
  SCF runs, and the calculation's identity is unchanged because the calculation is unchanged.

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
from rdkit import Chem

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
    "SiteReactivityResult",
    "compute_fukui",
    "compute_properties",
    "fukui_inputs",
    "properties_inputs",
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

    `sites` is ordered most-susceptible first by the index named in `ranked_by`, and holds **one
    entry per atom** — `len(sites) == total_atoms`, always. That is what makes `ranked_for` sound
    and what a caller's cache needs: `mode` is outside this calculation's key, so a stored row has
    to answer every mode's ranking, and a row shortened to the interesting few cannot. Presenting a
    shortlist is the caller's step. The ranking is valid *within* this molecule
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
    descriptors: GlobalDescriptors
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


def _wiberg(matrix: np.ndarray) -> np.ndarray:
    """The Wiberg bond-order matrix, with tblite's trailing spin dimension dropped.

    One channel here: this server runs no spin-polarised property calculation, and taking `[:, :,
    0]`
    in two places is how the two would silently disagree if one ever did.
    """
    return np.asarray(matrix)[:, :, 0]


def _valences(
    wiberg: np.ndarray, symbols: list[str], charges: list[int]
) -> list[tuple[float, float | None]]:
    """Each atom's total Wiberg valence and its free valence.

    Free valence is the classical Coulson radical index: the element's normal valence minus the bond
    order the atom actually uses.

    **It is `None` wherever the atom has no single normal valence to subtract from**, which is what
    RDKit's
    valence *list* answers and its `GetDefaultValence` does not. Measured: sulfur's default is 2,
    so a sulfone's sulfur — which uses 4.94 of Wiberg bond order — came out at a free valence of
    **-2.94**, a number that looks like a strongly saturated atom and means nothing at all. The list
    is `[2, 4, 6]` there, and no single member of it is "the" normal valence, so the honest answer
    is
    that this element has no free valence rather than a negative one.
    """
    table = Chem.GetPeriodicTable()
    used = wiberg.sum(axis=1)
    valences: list[tuple[float, float | None]] = []
    for symbol, charge, total in zip(symbols, charges, used, strict=True):
        allowed = list(table.GetValenceList(symbol))
        # Two ways an atom has no single normal valence to subtract from, and both were measured.
        undefined = len(allowed) != 1 or charge != 0
        free = None if undefined else round(allowed[0] - float(total), 3)
        valences.append((round(float(total), 3), free))
    return valences


def _formal_charges(structure: Structure) -> list[int]:
    """Per-atom formal charges for `structure`, read back from the canonical SMILES it carries.

    `Structure` records the *molecular* charge and not where it sits, which is enough for an SCF and
    not enough to say whether one atom is in its neutral normal-valence state. The canonical SMILES
    is: `structure_from_smiles` stores it, and `AddHs` over it reproduces this structure's atom
    order exactly — heavy atoms in canonical order, hydrogens appended by parent — which is the same
    alignment every other per-atom field here relies on.

    **Returns all-unknown (a non-zero sentinel) when there is no SMILES**, so a geometry-only
    structure yields no free valence rather than one computed against an assumption. That is the
    honest failure: `compute_properties_at` takes a bare geometry, and guessing neutrality there
    would put the sulfone number back for a case nobody could see.
    """
    if structure.smiles is None:
        return [1] * len(structure.elements)
    molecule = Chem.AddHs(Chem.MolFromSmiles(structure.smiles))
    charges = [atom.GetFormalCharge() for atom in molecule.GetAtoms()]
    # A mismatch means the SMILES does not describe this geometry; refuse the descriptor rather
    # than pair charges with the wrong atoms.
    return charges if len(charges) == len(structure.elements) else [1] * len(structure.elements)


def _bond_orders(matrix: np.ndarray, threshold: float) -> list[BondOrder]:
    """Upper-triangle bond orders above `threshold`, strongest first."""
    wiberg = _wiberg(matrix)
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
        bond_orders=_bond_orders(result["bond-orders"], settings.xtb_bond_order_threshold),
    )


def _global_descriptors(neutral: float, cation: float, anion: float) -> GlobalDescriptors:
    """The conceptual-DFT panel from three total energies in Hartree.

    Raises `ValueError` on a non-positive hardness. `eta = IP - EA` divides both the softness and
    the electrophilicity, and a zero or negative value means the three SCFs did not describe one
    consistent electronic system — returning an infinity or a sign-flipped electrophilicity from it
    would hand a caller a number that looks like a descriptor.
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

    **Every derived value is computed from the *rounded* f-minus and f-plus, not from the raw
    differences**, so a caller who recomputes `(f_minus + f_plus) / 2` from the two numbers this
    result carries gets the number it also carries. Rounding the derivation separately left
    `f_zero` disagreeing with its own definition in the fourth decimal — small, and exactly the kind
    of inconsistency that makes a reader distrust the rest of the panel.
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

    Runs three single points on the *same* geometry — the neutral molecule and its one-electron
    oxidized and reduced forms — and takes the Mulliken charge differences. In terms of charges q
    (electron population is Z - q):

        f-(k) = q(k, N-1) - q(k, N)     electrophilic attack
        f+(k) = q(k, N)   - q(k, N+1)   nucleophilic attack
        f0(k) = (f- + f+) / 2           radical attack

    **The same three results also carry their total energies, and reading them is what produces the
    global panel.** `IP = E(N-1) - E(N)` and `EA = E(N) - E(N+1)` are vertical Delta-SCF quantities
    on a fixed geometry; chemical potential, hardness, softness and electrophilicity follow from
    them, and local softness and local electrophilicity from those in turn. Every one of those
    numbers used to be computed and thrown away — this function read `["charges"]` out of each
    result and nothing else. No fourth single point runs, which is why the calculation's identity is
    unchanged: the calculation did not change, only how much of its result is read.

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


def properties_inputs(smiles: str, solvent: str | None = None) -> tuple[XtbSpec, Structure]:
    """The settings and the geometry `compute_properties` runs on — see `xtb.sp_inputs` for why.

    The solvent is validated here, at spec construction, so an unparameterised name is refused
    before any geometry is embedded — which also means `calculation_identity` refuses it for the
    same reason and with the same message as the compute path.
    """
    return XtbSpec(task="properties", solvent=solvent), property_structure(smiles)


def fukui_inputs(smiles: str, solvent: str | None = None) -> tuple[XtbSpec, Structure]:
    """The settings and the geometry `compute_fukui` runs on.

    `mode` is deliberately absent: the three single points do not depend on it — it only chooses the
    sort — so it is not part of the calculation's identity. That is the same split Chemclaw3's cache
    made, and it is what lets a caller ask for a second ranking without a second calculation.
    """
    return XtbSpec(task="fukui", solvent=solvent), property_structure(smiles)


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
