"""Per-atom descriptors only the `xtb` binary can produce, and an honest refusal when it is absent.

**Why this is a separate module rather than more of `xtb_props`.** Everything in `xtb_props` runs
in-process through tblite. Everything here needs the *binary*, because the four quantities it
returns are not on `tblite.Result` at all — measured against tblite 0.7.0, which exposes `energy`,
`charges`, `bond-orders`, `dipole`, `quadrupole`, the orbital energies and occupations, the orbital
coefficients and the density matrix, and nothing else. There is no overlap matrix and there are no
atomic multipoles, so a Mulliken-condensed frontier density cannot be formed either.

What the binary adds, all of it read off a real 6.6.1 run rather than off the documentation:

- **The covalent coordination number, the C6 dispersion coefficient and the static isotropic
  polarisability**, per atom, from the property table xtb prints on every run.
- **Atomic dipole and quadrupole moments**, from `xtbout.json` — GFN2's anisotropic electrostatics,
  which is the part of the Hamiltonian a plain partial charge throws away.
- **The electrostatic potential on a molecular surface**, from a second `--esp` run, reduced to its
  extrema. That is where a sigma-hole shows up, and a partial charge cannot show one at all.

**Fukui indices are deliberately not taken from here**, although the binary computes them with
`--vfukui`. `xtb_props.compute_fukui` already answers that question, and a second implementation
would be a second answer to it — the failure `connectors/README.md` records as two live definitions
of `predict_pka`. The two are not even the same quantity: measured, xtb's `--vfukui` reports all
three indices as *negative* for phenol where the finite-difference definition here reports them
positive, because it differentiates charge where this differentiates population.

**When the binary is absent this refuses by name.** It does not fall back, and it does not return a
partial payload with nulls where the descriptors would be: a caller cannot tell "this deployment has
no xtb" from "this atom has no polarisability" by looking at a null, and the first is an operator
fact while the second is a chemical claim.
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

    Atom indices match `atoms`' order, which is the structure's, which is the canonical SMILES'
    heavy atoms followed by their hydrogens — so this panel joins onto `ElectronicProperties` and
    `SiteReactivityResult` for the same structure by index.
    """

    smiles: str | None
    structure_id: str
    method: str
    solvent: str | None
    total_energy_hartree: float
    atoms: list[AtomicDescriptor]


class SurfacePotentialResult(Keyed):
    """The electrostatic-potential extrema on a molecular surface, for one geometry.

    **A separate calculation with its own key, not a flag on the panel above**, and the reason is
    this repository's own primitive rule rather than tidiness. An `--esp` run is a *second* SCF: on
    xtb 6.6.1 it writes the grid and then aborts during teardown before `xtbout.json` exists, so it
    cannot also deliver the atomic multipoles. Folding it into `compute_atomic_descriptors` as an
    argument therefore made one cache row stand for two different payloads — and since the argument
    could not enter the key without recomputing the panel, a `surface=True` call would have been
    served the earlier `surface=False` row and returned `surface: null` having run nothing. Two
    primitives, two keys, and the caller composes.
    """

    smiles: str | None
    structure_id: str
    method: str
    solvent: str | None
    surface: xtb_cli.SurfacePotential


def require_binary() -> None:
    """Refuse, by name, when the `xtb` binary this module is entirely built on is not installed.

    A `ValueError` on purpose: `mcp_server_kit.connector_app` lets that family reach the model
    verbatim and replaces everything else with a generic notice, and this message has to reach the
    chemist — it is the difference between "this deployment cannot answer that" and "this molecule
    has no answer".
    """
    if not xtb_cli.is_available():
        raise ValueError(
            "atomic polarisabilities, dispersion coefficients and atomic multipoles require the "
            "'xtb' binary, which is not installed in this deployment. Nothing here approximates "
            "them: tblite exposes no atomic multipoles and no polarisability, so there is no "
            "in-process fallback to fall back to. The partial charges, bond orders and Fukui "
            "indices from compute_electronic_properties and predict_site_reactivity do not need it."
        )


def atomic_inputs(smiles: str, solvent: str | None = None) -> tuple[XtbSpec, Structure]:
    """The settings and the geometry `compute_atomic_descriptors` runs on.

    `engine="xtb"` is stated rather than resolved, because this calculation has exactly one possible
    backend. That also keeps `calc_version` honest: it names the binary that really produced the
    numbers, so a deployment without one gets the refusal above instead of a key that claims a
    program it does not have.

    The geometry is `property_structure`'s, the same MMFF-relaxed embedding the other two per-atom
    calculators use, so a caller may join a polarisability onto a Fukui index for the same atom of
    the same structure without a second embedding.

    **This derives a key even where no binary is installed**, naming `xtb-absent`, which is the same
    thing the two CREST searches do and for the same reason: deriving an identity is not running a
    calculation, and `calculation_key` exists precisely so a caller can ask *before* committing.
    The refusal belongs at the point of compute, where it is actionable, not at the point of asking.
    """
    return XtbSpec(task="atomic", engine="xtb", solvent=solvent), property_structure(smiles)


def surface_inputs(smiles: str, solvent: str | None = None) -> tuple[XtbSpec, Structure]:
    """The settings and the geometry `compute_surface_potential` runs on.

    The same geometry `atomic_inputs` and the two tblite per-atom calculators use, so a caller may
    put a surface extremum beside a polarisability for the same structure without a second
    embedding — and a different `task`, so the two calculations are two cache rows.
    """
    return XtbSpec(task="surface", engine="xtb", solvent=solvent), property_structure(smiles)


def compute_surface_potential(spec: XtbSpec, structure: Structure) -> SurfacePotentialResult:
    """Compute the molecular electrostatic potential and return its extrema.

    Raises:
        ValueError: the binary is absent, or the spec did not resolve to it.
        CliError: the run failed or produced no grid.
    """
    resolved = _require_binary_backend(spec, structure)
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


def _require_binary_backend(spec: XtbSpec, structure: Structure) -> XtbSpec:
    """Resolve `spec` and refuse unless the binary really is what will run it."""
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
    resolved = _require_binary_backend(spec, structure)
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
    """The Euclidean magnitude of one atom's multipole, or None when the run did not report it.

    Not `numpy.linalg.norm`: this is a three- or six-component list off a JSON document, and
    reaching for an array library to add six squares would be the more complicated way to do it.
    """
    if index >= len(vectors):
        return None
    total = sum(float(component) * float(component) for component in vectors[index])
    # `math.sqrt`, not `** 0.5`: the operator is typed as returning `Any` because a float power can
    # be complex, and an `Any` leaking out of a typed helper is the thing --strict is for.
    return round(math.sqrt(total), 6)
