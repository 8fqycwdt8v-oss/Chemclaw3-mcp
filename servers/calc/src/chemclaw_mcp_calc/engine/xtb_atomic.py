"""Per-atom descriptors only the `xtb` binary can produce, and a refusal by name when it is absent.

`tblite.Result` exposes no coordination numbers, C6 coefficients, polarisabilities, atomic
multipoles or overlap matrix, so these need the binary:

- coordination number, C6 and isotropic polarisability per atom, from xtb's property table;
- atomic dipole and quadrupole moments, from `xtbout.json`;
- surface electrostatic-potential extrema, from a separate `--esp` run (where a sigma-hole shows).

Fukui indices are not taken from `--vfukui`: `xtb_props.compute_fukui` already answers that, and
xtb's quantity differentiates charge rather than population. No fallback and no partial payload
of nulls: "this deployment has no xtb" is an operator fact, not a chemical claim.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, Field

from chemclaw_mcp_calc.engine import xtb_cli
from chemclaw_mcp_calc.engine.key import Keyed
from chemclaw_mcp_calc.engine.structure import Structure
from chemclaw_mcp_calc.engine.xtb_props import property_structure
from chemclaw_mcp_calc.engine.xtb_spec import XtbSpec

__all__ = [
    "AtomicDescriptor",
    "AtomicDescriptorResult",
    "SurfacePotentialResult",
    "atomic_inputs",
    "compute_atomic_descriptors",
    "compute_surface_potential",
    "require_binary",
    "surface_inputs",
]


class AtomicDescriptor(BaseModel):
    """One atom's polarisability, dispersion and multipole descriptors, in atomic units.

    `dipole_norm_au` and `quadrupole_norm_au` are the magnitudes of this atom's own multipole
    moments — the anisotropy a partial charge cannot carry. A large atomic dipole on a halogen or a
    carbonyl oxygen is the lone-pair/sigma-hole structure that decides a halogen bond or a close
    contact, and it is zero-by-construction in any point-charge picture.
    """

    index: int
    element: str
    coordination_number: float = Field(
        description="Fractional covalent coordination number the GFN Hamiltonian uses."
    )
    charge: float = Field(description="Mulliken partial charge, as the binary reports it.")
    c6_au: float = Field(description="Atomic C6 dispersion coefficient, in atomic units.")
    polarisability_au: float = Field(
        description="Static isotropic atomic polarisability alpha(0), in atomic units."
    )
    dipole_norm_au: float | None = None
    quadrupole_norm_au: float | None = None


class AtomicDescriptorResult(Keyed):
    """The binary-only per-atom panel for one geometry.

    Atom indices follow the structure (canonical heavy atoms, then hydrogens), so this joins onto
    `ElectronicProperties` and `SiteReactivityResult` for the same structure by index.
    """

    smiles: str | None
    structure_id: str
    method: str
    solvent: str | None
    total_energy_hartree: float
    atoms: list[AtomicDescriptor]


class SurfacePotentialResult(Keyed):
    """The electrostatic-potential extrema on a molecular surface, for one geometry.

    A separate primitive with its own key, not a flag on the panel: an `--esp` run is a second SCF
    that cannot also deliver the multipoles, and one cache row must stand for one payload.
    """

    smiles: str | None
    structure_id: str
    method: str
    solvent: str | None
    surface: xtb_cli.SurfacePotential


def require_binary() -> None:
    """Refuse, by name, when the `xtb` binary this module is entirely built on is not installed.

    Names both calculations, since it is raised for both and by `calculation_key` too. A
    `ValueError` so `connector_app` passes it to the model verbatim: "this deployment cannot answer"
    is not "this molecule has no answer".
    """
    if not xtb_cli.is_available():
        raise ValueError(
            "the per-atom panel and the surface potential require the 'xtb' binary, which is not "
            "installed in this deployment. Nothing here approximates them: tblite exposes no "
            "atomic multipoles, no polarisability and no potential grid, so there is no "
            "in-process fallback to fall back to. The partial charges, bond orders and Fukui "
            "indices from compute_electronic_properties and predict_site_reactivity do not need it."
        )


def atomic_inputs(smiles: str, solvent: str | None = None) -> tuple[XtbSpec, Structure]:
    """The settings and the geometry `compute_atomic_descriptors` runs on.

    `engine="xtb"` is stated, not resolved: there is one possible backend, and `calc_version` must
    name the program that really produced the numbers. The geometry is `property_structure`'s,
    shared with the other per-atom calculators so results join by atom. No key is derived where no
    binary is installed: `identity.py` refuses through `require_binary_backend`, exactly where
    computing would.
    """
    return XtbSpec(task="atomic", engine="xtb", solvent=solvent), property_structure(smiles)


def surface_inputs(smiles: str, solvent: str | None = None) -> tuple[XtbSpec, Structure]:
    """The settings and the geometry `compute_surface_potential` runs on.

    The same geometry as `atomic_inputs`, with a different `task`, so the two are separate cache
    rows.
    """
    return XtbSpec(task="surface", engine="xtb", solvent=solvent), property_structure(smiles)


def compute_surface_potential(spec: XtbSpec, structure: Structure) -> SurfacePotentialResult:
    """Compute the molecular electrostatic potential and return its extrema.

    Raises:
        ValueError: the binary is absent, or the spec did not resolve to it.
        CliError: the run failed or produced no grid.
    """
    resolved = require_binary_backend(spec, structure)
    return SurfacePotentialResult(
        calc_version=resolved.calc_version(),
        calc_key=resolved.cache_key(structure).as_str(),
        smiles=structure.smiles,
        structure_id=structure.structure_id,
        method=resolved.method,
        solvent=resolved.solvent,
        surface=xtb_cli.run_surface_potential(
            structure,
            method=resolved.method,
            solvent=resolved.solvent,
            accuracy=resolved.accuracy,
        ),
    )


def require_binary_backend(spec: XtbSpec, structure: Structure) -> XtbSpec:
    """Resolve `spec` and refuse unless the binary really is what will run it.

    Public because `engine/identity.py` calls it too: `calculation_key` must refuse on exactly the
    conditions the compute path does (binary absent, open-shell fallback), or it would answer a key
    nothing will ever write.
    """
    require_binary()
    resolved = spec.for_structure(structure)
    if resolved.engine != "xtb":
        raise ValueError(
            f"this calculation needs the xtb binary; the spec resolved to {resolved.engine!r}. "
            "An open-shell structure resolves to tblite deliberately — the 6.6.1 binary's "
            "--spinpol is killed by the OOM killer — so these panels are closed-shell only."
        )
    return resolved


def compute_atomic_descriptors(spec: XtbSpec, structure: Structure) -> AtomicDescriptorResult:
    """Run the binary once and read every per-atom quantity tblite cannot produce.

    Args:
        spec: The settings; its `engine` must resolve to the binary.
        structure: The geometry to compute on.

    Raises:
        ValueError: the binary is absent, or the spec did not resolve to it.
        CliError: the run failed or produced no property table.
    """
    resolved = require_binary_backend(spec, structure)
    result = xtb_cli.run(
        structure,
        task="sp",
        method=resolved.method,
        solvent=resolved.solvent,
        accuracy=resolved.accuracy,
    )
    if not result.atomic_rows:
        raise xtb_cli.CliError(
            "xtb produced no per-atom property table, so there is nothing to report"
        )
    dipoles = result.properties.get("atomic dipole moments") or []
    quadrupoles = result.properties.get("atomic quadrupole moments") or []
    return AtomicDescriptorResult(
        calc_version=resolved.calc_version(),
        calc_key=resolved.cache_key(structure).as_str(),
        smiles=structure.smiles,
        structure_id=structure.structure_id,
        method=resolved.method,
        solvent=resolved.solvent,
        total_energy_hartree=result.energy_hartree,
        atoms=[
            AtomicDescriptor(
                index=row.index,
                element=row.element,
                coordination_number=row.coordination_number,
                charge=row.charge,
                c6_au=row.c6_au,
                polarisability_au=row.polarisability_au,
                dipole_norm_au=_norm(dipoles, row.index),
                quadrupole_norm_au=_norm(quadrupoles, row.index),
            )
            for row in result.atomic_rows
        ],
    )


def _norm(vectors: list[list[float]], index: int) -> float | None:
    """The Euclidean magnitude of one atom's multipole, or None when the run did not report it."""
    if index >= len(vectors):
        return None
    total = sum(float(component) * float(component) for component in vectors[index])
    # `math.sqrt`, not `** 0.5`: the operator is typed as returning `Any` because a float power can
    # be complex, and an `Any` leaking out of a typed helper is the thing --strict is for.
    return round(math.sqrt(total), 6)
