"""Shared GFN2-xTB engine primitives: RDKit geometry + the tblite single point.

Used by every xTB-based calculator here (`xtb`, `pka`, `xtb_props`, `xtb_opt`, `xtb_hessian`) so
the embed/SCF plumbing exists once. Geometry generation is deterministic via a caller-supplied
seed; single points optionally use ALPB implicit solvation.

This module is the **unit boundary**: everything above it works in Angstrom (the interchange unit
of RDKit, XYZ files, and `structure.Structure`), and the conversion to the atomic units tblite
wants happens here and nowhere else.

`engine_version()` is also the first half of every `calc_version` this server emits, which is the
reason the whole calc bundle was ported rather than left behind: the distributions it reads are
installed *here*.
"""

from __future__ import annotations

from importlib.metadata import version
from typing import Any

import numpy as np
from rdkit import Chem
from rdkit.Chem import AllChem
from tblite.interface import Calculator

from chemclaw_mcp_calc.engine.solvents import SUGGESTED_SOLVENTS

# Re-exported: `xtb_opt` annotates the calculator it passes between its own helpers, and this module
# is the single place tblite is imported from (the unit boundary).
__all__ = [
    "ANGSTROM_TO_BOHR",
    "AU_TO_DEBYE",
    "HARTREE_TO_KCAL",
    "Calculator",
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

# tblite works in atomic units; everything above this module is in Angstrom.
ANGSTROM_TO_BOHR = 1.8897259886

# CODATA Hartree-to-kcal/mol, to full double precision. Every calculator here that reports a
# relative or interaction energy in kcal/mol converts through this single value, so a truncated copy
# cannot drift from the rest.
HARTREE_TO_KCAL = 627.5094740631

# CODATA atomic-unit-to-Debye, for the same reason and by the same rule as the line above: every
# module that reports a dipole or a dipole derivative in Debye converts through this one value. It
# was three literals in three modules in Chemclaw3, one of which sat inside the module that does the
# unit arithmetic and waited to be applied a second time to numbers already converted.
AU_TO_DEBYE = 2.5417464519

# The tblite result properties any calculator here reads. Named explicitly rather than taking the
# whole result: it also carries the density matrix and orbital coefficients, which nothing consumes
# and which scale as the square of the basis size.
_CONSUMED_PROPERTIES = (
    "energy",
    "charges",
    "bond-orders",
    "dipole",
    "orbital-energies",
    "orbital-occupations",
)


# Revision of the *Hamiltonian settings* this engine applies, independent of the tblite build.
# Bumped when a change to how a calculation is set up moves numbers — the spin-polarization
# contribution added for open-shell systems is revision 2. Without a tag like this, such a change is
# invisible to the cache key and old entries would be served for a physics the current code would
# not reproduce.
#
# **This constant is half of a shared contract with Chemclaw3's cache and its calibration ledger.**
# It appears verbatim in every `calc_version` string this server emits; the rows keyed by those
# strings live over there. Bumping it here is a deliberate invalidation of that history, exactly as
# it was when the two lived in one repository.
_HAMILTONIAN_REVISION = "h2"


def engine_version() -> str:
    """The installed tblite and RDKit builds, for embedding in calculation versions.

    Every `calc_version` of a calculator that runs this engine (xTB energy, properties, Fukui,
    optimization, Hessian, pKa) must include both so an upgrade of either — tblite shifts energies,
    RDKit shifts the seeded ETKDG embedding and MMFF geometries — is a cache miss on the Chemclaw3
    side, not a silent stale hit. Widening the version string invalidates existing entries; that is
    correct, as those did not record the geometry stack that produced them.

    **This is the value a Chemclaw3 pod cannot compute.** Neither distribution is installed there
    after the split, so `version('tblite')` raises `PackageNotFoundError` rather than returning
    something wrong — which is the *good* failure. The bad one is `xtb_cli.binary_version()`, which
    returns `"absent"` instead of raising. Either way, the derivation belongs where the programs
    are.
    """
    return f"tblite-{version('tblite')}/rdkit-{version('rdkit')}/{_HAMILTONIAN_REVISION}"


def parse_molecule(smiles: str) -> Chem.Mol:
    """Parse a SMILES into a molecule with explicit hydrogens, or raise."""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"invalid SMILES: {smiles!r}")
    return Chem.AddHs(mol)


def require_closed_shell(mol: Chem.Mol, charge: int) -> None:
    """Reject odd-electron (open-shell) species with a `ValueError`.

    tblite converges odd-electron systems via fractional occupation without any error, returning an
    energy for an ill-defined electronic state, and a SMILES does not encode the true spin
    multiplicity — so a caller who has only a SMILES has nothing honest to pass as `uhf` and failing
    fast is the right contract. Expects explicit hydrogens (`parse_molecule` output) so the electron
    count is complete.

    Kept for `pka`, whose calibration is defined over neutral closed-shell acids. Callers that *can*
    state a multiplicity use `structure.Structure` instead, which validates the electron count
    against it rather than refusing every open shell — that is what makes the Fukui ions computable.

    Documented limit: this catches *odd*-electron species only. An **even**-electron open shell —
    triplet dioxygen is the canonical case — is undetectable from a SMILES, which carries no spin
    multiplicity, so it passes here and is treated as a singlet. That is a property of the input
    format, not of this check.
    """
    electrons = sum(atom.GetAtomicNum() for atom in mol.GetAtoms()) - charge
    if electrons % 2:
        raise ValueError(
            f"open-shell species ({electrons} electrons at charge {charge}) is not "
            "supported: GFN2-xTB here is closed-shell only"
        )


def conformer_positions(mol: Chem.Mol, conf_id: int = -1) -> tuple[np.ndarray, np.ndarray]:
    """Extract (atomic numbers, positions in **Angstrom**) from an embedded conformer on `mol`.

    Reads one conformer of an already-embedded molecule by id, so a caller that embedded several up
    front gets each without re-embedding. The name states the unit deliberately: two functions here
    disagreeing about their unit is precisely the bug this boundary exists to prevent.
    """
    conformer = mol.GetConformer(conf_id)
    numbers = np.array([atom.GetAtomicNum() for atom in mol.GetAtoms()])
    positions = np.array([list(conformer.GetAtomPosition(i)) for i in range(mol.GetNumAtoms())])
    return numbers, positions


def geometry(mol: Chem.Mol, seed: int, optimize: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """Embed a deterministic 3D geometry and return (atomic numbers, positions in Angstrom).

    Falls back to random-coordinate embedding if the default fails, then raises if that also fails.
    Optional MMFF pre-optimization is skipped when the force field lacks parameters for the molecule
    (a valid, common case) rather than erroring.
    """
    work = Chem.Mol(mol)  # copy so the caller's molecule gets no conformer
    # `type: ignore` on each `AllChem` call below is `rdkit-stubs`' doing rather than a claim
    # about the calls: `AllChem` re-exports its C++ symbols dynamically, so the stub package
    # declares almost none of them. Same convention as `servers/chem/engine/chem.py`.
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

    The same SCF that yields the total energy also yields Mulliken charges, Wiberg bond orders, the
    dipole, and the orbital energies — reading them out costs nothing, so this is the one entry
    point every xTB task uses and the energy-only `gfn2_energy` is a thin wrapper over it.

    `positions` is in **Angstrom** (see the module docstring); the conversion to atomic units
    happens here. `uhf` is the number of unpaired electrons, which the caller must state explicitly
    — tblite converges an odd-electron system silently at `uhf=0`, so an honest open-shell
    calculation depends on it being set.

    Args:
        method: GFN parametrization name, e.g. "GFN2-xTB".
        numbers: Atomic numbers, one per atom.
        positions: Cartesian coordinates in Angstrom, shape (natoms, 3).
        charge: Net molecular charge.
        uhf: Number of unpaired electrons (0 = closed shell).
        solvent: ALPB implicit solvent name, or None for gas phase.

    Returns:
        The consumed subset of the tblite result, as numpy arrays and scalars, in atomic units.
        Deliberately a subset: the full result also carries the density matrix and orbital
        coefficients, which nothing here reads and which are large.
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

    Exists so the tasks that evaluate the *same* system at many geometries — geometry optimization,
    the finite-difference Hessian — set the Hamiltonian up once and then call `energy_and_gradient`
    per step, instead of reconstructing a calculator per single point. `run_singlepoint` goes
    through it too, so the verbosity and solvation setup exist once.
    """
    calc = Calculator(method, numbers, positions * ANGSTROM_TO_BOHR, charge=charge, uhf=uhf)
    # tblite prints an SCF iteration table to stdout at its default verbosity, which would pollute
    # every request log and test run. It affects no numbers.
    calc.set("verbosity", 0)
    if uhf:
        # Without this, `uhf` only changes the *occupation*: the energy expression has no
        # spin-dependent term, so an open-shell state is not stabilized at all. Measured, and the
        # measurement is decisive — triplet O2 comes out 1.7 kcal/mol *above* singlet O2 without it
        # (qualitatively wrong; the triplet is the ground state) and 15.8 kcal/mol below it with
        # (experimental gap ~22). Enabled wherever there are unpaired electrons, with no scaling,
        # which is what `xtb --spinpol` does.
        calc.add("spin-polarization", 1.0)
    if solvent is not None:
        try:
            calc.add("alpb-solvation", solvent)
        except RuntimeError as error:
            # tblite's own message ("String value for epsilon was not found among database of
            # solvents") names an implementation detail rather than the mistake. This is the
            # *second* line of defence rather than the only one: `XtbSpec` refuses an unsupported
            # name at construction, so one reaching here came through a direct engine call. The two
            # share one shortlist so they cannot disagree about what the method supports.
            raise ValueError(
                f"unknown ALPB solvent {solvent!r}; common valid names are "
                f"{', '.join(SUGGESTED_SOLVENTS)}"
            ) from error
    return calc


def evaluate_point(calc: Calculator, positions: np.ndarray) -> tuple[float, np.ndarray, np.ndarray]:
    """Move `calc`'s system to `positions` (Angstrom) and evaluate it there.

    Returns `(energy, gradient, dipole)`: the energy in Hartree, the **analytic** gradient in
    Hartree/Angstrom, and the dipole in atomic units. tblite returns the gradient in Hartree/Bohr;
    the chain-rule factor for the conversion is the same constant that converts the coordinates,
    applied in the opposite direction.

    The dipole rides along because the SCF produced it anyway. Optimization discards it; the Hessian
    loop, which displaces every Cartesian and would otherwise need a second pass for dipole
    derivatives, gets IR intensities for free.

    An analytic gradient is what makes optimization cheap and puts the finite-difference Hessian at
    6N single points rather than 6N^2.
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
    """Return the GFN2-xTB total energy (Hartree) for a closed-shell system.

    Positions are in Angstrom. Closed-shell only by signature: callers needing an open-shell energy
    go through `run_singlepoint` and state `uhf` themselves.
    """
    result = run_singlepoint(method, numbers, positions, charge=charge, solvent=solvent)
    return float(result["energy"])
