"""One point of a relaxed scan — the step, not the sweep.

Chemclaw3's `xtb_scan.run_scan` walks a list of coordinate values and relaxes at each. **The loop is
not here.** What is here is one point: drive an internal coordinate to a value, freeze the atoms
that define it, and relax everything else.

The decomposition is exact rather than approximate, and that is a property of how the sweep was
written rather than a liberty taken here. `run_scan` drives every point from the **input** geometry
rather than from the previous point — deliberately, because a sequential scan's result depends on
the direction it was walked, which is a hidden input a content-addressed cache must not have. So the
points are independent by construction, and a sweep is exactly N calls to this.

**What the caller gains by composing it.** Chemclaw3 caches the sweep as one `xtb.scan` row today,
so adding two points to a 24-point profile recomputes all 26. Point by point, the 24 already
computed are hits. And a scan point is an ordinary constrained optimization, so it keys as `xtb.opt`
— which means a point shares a row with a hand-written constrained relaxation of the same geometry
rather than sitting in a private namespace.

**What stays behind with the sweep**: the profile arithmetic (relative energies against the lowest
point, the barrier maximum), the point-count cap, and picking the minimum geometry out. All of it is
arithmetic over what this returns.

Also not here: `progress.py`'s callback. A request/response tool has no channel to report progress
on, and inventing one would mean holding job state — which is the other side of the seam.
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

    A `Structure` holds elements and coordinates but no bonds, and setting an internal coordinate
    needs connectivity. Re-parsing the canonical SMILES reproduces the atom order the geometry was
    built in (`structure_from_smiles` embeds the same parse), and the element check turns that
    reliance into an assertion rather than an assumption.
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

    RDKit's `rdMolTransforms` sets a bond length, angle or dihedral by moving the whole attached
    fragment, so the driven geometry is chemically sensible rather than one atom dragged out of
    place. Deterministic, so the driven structure — and therefore the scan point's key — is a
    function of `(structure, atoms, value)` alone.

    Args:
        structure: The starting geometry, which must carry the SMILES its connectivity comes from.
        atoms: Two atoms for a bond, three for an angle, four for a dihedral. They must be bonded in
            sequence — RDKit rejects the rest.
        value: Angstrom for a bond, degrees for an angle or dihedral.

    Raises:
        ValueError: an atom index is out of range, the count is not 2-4, or the structure and its
            SMILES disagree.
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

    An ordinary `OptSpec` with the coordinate's defining atoms frozen, so a scan point *is* a
    constrained optimization and keys as `xtb.opt` rather than inventing a task of its own.

    The approximation this inherits: the frozen atoms' own local geometry — the bond lengths and
    angles *between* them — cannot relax with the coordinate. For a torsion profile, the case this
    is mostly used for, that is the standard treatment; for a bond-breaking scan the profile maximum
    is a sketch of a barrier rather than a transition state, because there is no saddle-point search
    anywhere in this system.
    """
    return OptSpec(solvent=solvent, frozen_atoms=tuple(atoms)), drive_coordinate(
        structure, tuple(atoms), value
    )
