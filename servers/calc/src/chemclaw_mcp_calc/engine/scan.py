"""One point of a relaxed scan — the step, not the sweep.

Drive an internal coordinate to a value, freeze the atoms defining it, relax the rest. Chemclaw3
drives every point from the input geometry (a sequential scan would depend on walk direction, a
hidden cache input), so a sweep is exactly N independent calls to this, each cached. A point keys
as `xtb.opt`, sharing rows with an equivalent constrained relaxation. The profile arithmetic, the
point cap and progress reporting stay with the sweep in Chemclaw3.
"""

from __future__ import annotations

from rdkit import Chem
from rdkit.Chem import rdMolTransforms
from rdkit.Geometry import Point3D

from chemclaw_mcp_calc.engine.structure import Structure
from chemclaw_mcp_calc.engine.xtb_engine import parse_molecule
from chemclaw_mcp_calc.engine.xtb_opt import OptSpec

__all__ = ["COORDINATES", "drive_coordinate", "scan_point_inputs"]

# How many atoms define each internal coordinate, and the unit its value is in.
COORDINATES: dict[int, tuple[str, str]] = {
    2: ("bond", "angstrom"),
    3: ("angle", "degree"),
    4: ("dihedral", "degree"),
}


def _mol_with_conformer(structure: Structure) -> Chem.Mol:
    """Rebuild the RDKit molecule for `structure`, carrying its geometry.

    Setting an internal coordinate needs bonds, which a `Structure` lacks; re-parsing the canonical
    SMILES reproduces the embedding's atom order, and an element check enforces that.
    """
    if not structure.smiles:
        raise ValueError("a scan point needs the molecule's SMILES to know its connectivity")
    mol = parse_molecule(structure.smiles)
    elements = [atom.GetAtomicNum() for atom in mol.GetAtoms()]
    if elements != structure.elements:
        raise ValueError("structure does not match its SMILES: atom order or composition differs")
    conformer = Chem.Conformer(len(elements))
    for index, (x, y, z) in enumerate(structure.positions):
        conformer.SetAtomPosition(index, Point3D(x, y, z))
    mol.AddConformer(conformer, assignId=True)
    return mol


def drive_coordinate(structure: Structure, atoms: tuple[int, ...], value: float) -> Structure:
    """Move one internal coordinate of `structure` to `value`. Pure geometry, no SCF.

    `atoms` are two (bond, Angstrom), three (angle, degrees) or four (dihedral, degrees) atoms
    bonded in sequence; `structure` must carry its SMILES. `rdMolTransforms` moves the whole
    attached fragment, deterministically. Raises `ValueError` for a bad index or count, or a
    structure that disagrees with its SMILES.
    """
    if len(atoms) not in COORDINATES:
        raise ValueError(
            f"an internal coordinate is defined by 2 atoms (bond), 3 (angle) or 4 (dihedral); "
            f"{len(atoms)} were given"
        )
    if max(atoms) >= len(structure.elements) or min(atoms) < 0:
        raise ValueError(f"scan atom index out of range for {len(structure.elements)} atoms")
    mol = _mol_with_conformer(structure)
    conformer = Chem.Conformer(mol.GetConformer())
    # Indexed rather than `*atoms`-unpacked: the arity is what distinguishes the three setters, and
    # spelling it out is what lets a type checker see that the right one gets the right count.
    if len(atoms) == 2:
        rdMolTransforms.SetBondLength(conformer, atoms[0], atoms[1], value)
    elif len(atoms) == 3:
        rdMolTransforms.SetAngleDeg(conformer, atoms[0], atoms[1], atoms[2], value)
    else:
        rdMolTransforms.SetDihedralDeg(conformer, atoms[0], atoms[1], atoms[2], atoms[3], value)
    return Structure(
        elements=structure.elements,
        positions=[list(conformer.GetAtomPosition(i)) for i in range(len(structure.elements))],
        charge=structure.charge,
        multiplicity=structure.multiplicity,
        smiles=structure.smiles,
    )


def scan_point_inputs(
    structure: Structure,
    atoms: tuple[int, ...],
    value: float,
    solvent: str | None = None,
) -> tuple[OptSpec, Structure]:
    """The settings and driven geometry one scan point relaxes — the pair its identity is made of.

    An ordinary `OptSpec` with the defining atoms frozen, so it keys as `xtb.opt`. The frozen atoms'
    mutual geometry cannot relax: standard for torsion profiles, but for a bond-breaking scan the
    maximum only sketches a barrier (there is no saddle-point search).
    """
    return OptSpec(solvent=solvent, frozen_atoms=tuple(atoms)), drive_coordinate(
        structure, tuple(atoms), value
    )
