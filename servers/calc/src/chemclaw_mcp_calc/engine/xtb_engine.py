"""Shared GFN2-xTB engine primitives: RDKit geometry + the tblite single point.

Used by every xTB-based calculator here, so the embed/SCF plumbing exists once. Geometry is
deterministic via a caller-supplied seed; single points optionally use ALPB implicit solvation.

This module is the **unit boundary**: everything above it works in Angstrom, and conversion to
tblite's atomic units happens here and nowhere else. `engine_version()` is the first half of every
`calc_version` this server emits.
"""

from __future__ import annotations

from importlib.metadata import version
from typing import Any

import numpy as np
from mcp_server_kit.limits import echo
from rdkit import Chem
from rdkit.Chem import AllChem
from scipy import constants
from tblite.interface import Calculator

from chemclaw_mcp_calc.engine.config import settings
from chemclaw_mcp_calc.engine.solvents import SUGGESTED_SOLVENTS

# Re-exported: `xtb_opt` annotates the calculator it passes between its own helpers, and this module
# is the single place tblite is imported from (the unit boundary).
__all__ = [
    "ANGSTROM_TO_BOHR",
    "AU_TO_DEBYE",
    "HARTREE_TO_KCAL",
    "Calculator",
    "atom_ceiling_error",
    "conformer_positions",
    "engine_version",
    "evaluate_point",
    "geometry",
    "gfn2_energy",
    "make_calculator",
    "parse_molecule",
    "require_closed_shell",
    "run_singlepoint",
]

# Unit conversions, derived from `scipy.constants` rather than transcribed. They track the
# installed CODATA table, so the scipy version is part of `engine_version()` and a new table is a
# cache miss rather than a silent shift in far decimals.

# tblite works in atomic units; everything above this module is in Angstrom. One value, no copies.
ANGSTROM_TO_BOHR = 1e-10 / constants.value("Bohr radius")

# Hartree to kcal/mol, the single value every calculator converts through. `4184` J is the
# thermochemical calorie by definition.
HARTREE_TO_KCAL = constants.value("Hartree energy") * constants.Avogadro / 4184.0

# Atomic units to Debye, the single value every dipole conversion goes through. The debye is
# exactly 1e-21/c coulomb-metres.
AU_TO_DEBYE = constants.value("atomic unit of electric dipole mom.") * constants.c / 1e-21

# The tblite result properties any calculator reads; the density matrix and orbital coefficients
# are skipped because nothing uses them and they scale with the basis size squared.
_CONSUMED_PROPERTIES = (
    "energy",
    "charges",
    "bond-orders",
    "dipole",
    "orbital-energies",
    "orbital-occupations",
)


# Revision of the Hamiltonian *settings* this engine applies, independent of the tblite build.
# Bump it when a set-up change moves numbers, or old cache entries are served for physics the code
# no longer reproduces. It appears verbatim in every `calc_version`, a contract with Chemclaw3's
# cache and calibration ledger, so bumping it deliberately invalidates that history.
# h2: spin polarization for open shells. h3: unit conversions derived from `scipy.constants`.
_HAMILTONIAN_REVISION = "h3"


def engine_version() -> str:
    """The installed tblite, RDKit and scipy builds, for embedding in calculation versions.

    An upgrade of any one moves results (tblite energies, RDKit embeddings, scipy's CODATA table
    behind the unit conversions), so each must be a cache miss on the Chemclaw3 side. The optimizer
    (geomeTRIC) is deliberately absent: this string also keys calculations that run no optimizer,
    and `OptSpec.calc_version()` names it. Derived here, where the packages are installed; a
    Chemclaw3 pod cannot compute it.
    """
    return (
        f"tblite-{version('tblite')}/rdkit-{version('rdkit')}"
        f"/scipy-{version('scipy')}/{_HAMILTONIAN_REVISION}"
    )


def parse_molecule(smiles: str) -> Chem.Mol:
    """Parse a SMILES into a molecule with explicit hydrogens, or raise."""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"invalid SMILES: {echo(smiles)!r}")
    return Chem.AddHs(mol)


def require_closed_shell(mol: Chem.Mol, charge: int) -> None:
    """Reject odd-electron (open-shell) species with a `ValueError`.

    tblite converges an odd-electron system silently to an ill-defined state, and a SMILES carries
    no multiplicity, so failing fast is the honest contract. Expects explicit hydrogens. Kept for
    `pka` (calibrated on closed-shell acids); callers that can state a multiplicity use
    `structure.Structure`. Catches *odd*-electron species only: an even-electron open shell (triplet
    O2) is undetectable from a SMILES and is treated as a singlet.
    """
    electrons = sum(atom.GetAtomicNum() for atom in mol.GetAtoms()) - charge
    if electrons % 2:
        raise ValueError(
            f"open-shell species ({electrons} electrons at charge {charge}) is not "
            "supported: GFN2-xTB here is closed-shell only"
        )


def conformer_positions(mol: Chem.Mol, conf_id: int = -1) -> tuple[np.ndarray, np.ndarray]:
    """Extract (atomic numbers, positions in **Angstrom**) from an embedded conformer on `mol`.

    Reads one conformer by id, so several embedded up front need no re-embedding.
    """
    conformer = mol.GetConformer(conf_id)
    numbers = np.array([atom.GetAtomicNum() for atom in mol.GetAtoms()])
    positions = np.array([list(conformer.GetAtomPosition(i)) for i in range(mol.GetNumAtoms())])
    return numbers, positions


def atom_ceiling_error(atom_count: int, *, subject: str) -> str | None:
    """Why `atom_count` atoms is over `xtb_max_atoms`, or `None` — one wording for every check.

    Called by `geometry()` and `make_calculator()`, where the cost is paid, so nothing can embed or
    run xTB past the ceiling; `structure` re-exports it for its SMILES-echoing refusals.
    `atom_count` is hydrogen-inclusive.
    """
    if atom_count <= settings.xtb_max_atoms:
        return None
    return (
        f"{subject} of {atom_count} atoms exceeds this server's limit of "
        f"{settings.xtb_max_atoms}: every calculation here is at least one SCF over the "
        "whole system and runs inside a conversation turn, so a system this size is "
        "refused rather than started and abandoned. Run a smaller system, cut it to the "
        "region the question is about, or raise CHEMCLAW_XTB_MAX_ATOMS on a deployment "
        "with the memory for it — the ceiling is derived from this pod's own limit"
    )


def _atoms_with_hydrogens(mol: Chem.Mol) -> int:
    """`mol`'s atom count with every hydrogen counted, whether explicit atoms or implicit.

    So a caller that forgot `AddHs` is not under-counted.
    """
    return mol.GetNumAtoms() + sum(atom.GetTotalNumHs() for atom in mol.GetAtoms())


def geometry(mol: Chem.Mol, seed: int, optimize: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """Embed a deterministic 3D geometry and return (atomic numbers, positions in Angstrom).

    Falls back to random-coordinate embedding, then raises. MMFF pre-optimization is skipped when
    the force field lacks parameters. Refuses a molecule over `xtb_max_atoms` before embedding:
    every 3D embedding goes through here, so the ceiling holds even for callers that never build a
    `Structure`.
    """
    if reason := atom_ceiling_error(_atoms_with_hydrogens(mol), subject="a molecule"):
        raise ValueError(reason)
    work = Chem.Mol(mol)  # copy so the caller's molecule gets no conformer
    # The `type: ignore`s on `AllChem` calls are `rdkit-stubs` gaps (dynamic re-exports), not claims
    # about the calls.
    if (
        AllChem.EmbedMolecule(work, randomSeed=seed) != 0  # type: ignore[attr-defined]
        and AllChem.EmbedMolecule(  # type: ignore[attr-defined]
            work, randomSeed=seed, useRandomCoords=True
        )
        != 0
    ):
        raise ValueError("could not embed a 3D geometry")
    if optimize and AllChem.MMFFHasAllMoleculeParams(work):  # type: ignore[attr-defined]
        AllChem.MMFFOptimizeMolecule(work)  # type: ignore[attr-defined]
    return conformer_positions(work)


def run_singlepoint(
    method: str,
    numbers: np.ndarray,
    positions: np.ndarray,
    charge: int = 0,
    uhf: int = 0,
    solvent: str | None = None,
) -> dict[str, Any]:
    """Run one GFN single point and return every property the SCF produced.

    Charges, bond orders, dipole and orbital energies come free with the energy, so every xTB task
    uses this. `positions` is in Angstrom. `uhf` must be stated explicitly: tblite converges an
    odd-electron system silently at `uhf=0`.

    Args:
        method: GFN parametrization name, e.g. "GFN2-xTB".
        numbers: Atomic numbers, one per atom.
        positions: Cartesian coordinates in Angstrom, shape (natoms, 3).
        charge: Net molecular charge.
        uhf: Number of unpaired electrons (0 = closed shell).
        solvent: ALPB implicit solvent name, or None for gas phase.

    Returns:
        The consumed subset of the tblite result (`_CONSUMED_PROPERTIES`), in atomic units.
    """
    calc = make_calculator(method, numbers, positions, charge=charge, uhf=uhf, solvent=solvent)
    result = calc.singlepoint()
    return {key: result.get(key) for key in _CONSUMED_PROPERTIES}


def make_calculator(
    method: str,
    numbers: np.ndarray,
    positions: np.ndarray,
    charge: int = 0,
    uhf: int = 0,
    solvent: str | None = None,
) -> Calculator:
    """Build a configured tblite calculator for one system; `positions` in Angstrom.

    Lets optimization and the finite-difference Hessian set the Hamiltonian up once and call
    `energy_and_gradient` per step. Every SCF starts here, so it refuses a system over
    `xtb_max_atoms` as the backstop.
    """
    if reason := atom_ceiling_error(len(numbers), subject="a system"):
        raise ValueError(reason)
    calc = Calculator(method, numbers, positions * ANGSTROM_TO_BOHR, charge=charge, uhf=uhf)
    # tblite prints an SCF iteration table to stdout at its default verbosity, which would pollute
    # every request log and test run. It affects no numbers.
    calc.set("verbosity", 0)
    if uhf:
        # Without spin polarization `uhf` changes only the occupation and the triplet/singlet
        # ordering of O2 comes out wrong. Unscaled, as `xtb --spinpol` does.
        calc.add("spin-polarization", 1.0)
    if solvent is not None:
        try:
            calc.add("alpb-solvation", solvent)
        except RuntimeError as error:
            # Second line of defence (`XtbSpec` refuses first); replaces tblite's
            # implementation-detail message. Both share one shortlist.
            raise ValueError(
                f"unknown ALPB solvent {echo(solvent)!r}; common valid names are "
                f"{', '.join(SUGGESTED_SOLVENTS)}"
            ) from error
    return calc


def evaluate_point(calc: Calculator, positions: np.ndarray) -> tuple[float, np.ndarray, np.ndarray]:
    """Move `calc`'s system to `positions` (Angstrom) and evaluate it there.

    Returns `(energy, gradient, dipole)`: Hartree, the **analytic** gradient in Hartree/Angstrom
    (converted from Hartree/Bohr), and the dipole in atomic units. The dipole is free and gives the
    Hessian loop IR intensities; the analytic gradient makes the Hessian 6N single points, not 6N^2.
    """
    calc.update(positions=positions * ANGSTROM_TO_BOHR)
    result = calc.singlepoint()
    energy = float(result.get("energy"))
    gradient = np.asarray(result.get("gradient"), dtype=float) * ANGSTROM_TO_BOHR
    dipole = np.asarray(result.get("dipole"), dtype=float)
    return energy, gradient, dipole


def gfn2_energy(
    method: str,
    numbers: np.ndarray,
    positions: np.ndarray,
    charge: int = 0,
    solvent: str | None = None,
) -> float:
    """Return the GFN2-xTB total energy (Hartree) for a closed-shell system; positions in Angstrom.
    """
    result = run_singlepoint(method, numbers, positions, charge=charge, solvent=solvent)
    return float(result["energy"])
