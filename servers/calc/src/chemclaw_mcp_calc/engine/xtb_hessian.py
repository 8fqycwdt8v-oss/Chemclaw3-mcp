"""The Hessian: the expensive half of every vibrational question, kept as its own spec.

A Hessian depends only on the geometry and the method, not on temperature, pressure, symmetry
number or quasi-RRHO cutoff, so `HessianSpec` is narrower than `ThermoSpec` and thermochemistry
requests differing only in temperature share one cached Hessian on the Chemclaw3 side.

The matrix crosses the wire as base64 `.npy` (`pack_array`), the format Chemclaw3's
`calculation_artifacts` stores. At the default `xtb_hessian_max_atoms` a response is about
2.2 MB — above the request cap, which does not apply to responses, but worth knowing for a proxy.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from typing import Literal

import numpy as np
from pydantic import Field

from chemclaw_mcp_calc.engine import xtb_cli
from chemclaw_mcp_calc.engine.budget import Deadline
from chemclaw_mcp_calc.engine.config import settings
from chemclaw_mcp_calc.engine.structure import Structure
from chemclaw_mcp_calc.engine.xtb_engine import AU_TO_DEBYE, evaluate_point, make_calculator
from chemclaw_mcp_calc.engine.xtb_spec import XtbSpec

__all__ = ["Hessian", "HessianSpec", "compute_hessian", "pack_array", "unpack_array"]


class HessianSpec(XtbSpec):
    """Settings of one second-derivative calculation — everything that moves the matrix.

    Temperature, pressure, symmetry number and RRHO cutoff are absent on purpose: `cache_key` keys
    on `model_dump()`, so a field not here cannot force a recomputation.
    """

    task: Literal["hess"] = "hess"
    displacement_angstrom: float = Field(
        default_factory=lambda: settings.xtb_hessian_displacement, gt=0
    )


@dataclass(frozen=True)
class Hessian:
    """The second derivatives of one geometry, plus what the run collected alongside them.

    Not a pydantic model: it holds numpy arrays and never crosses the wire. Exactly one of
    `ir_intensities` (binary backend, one per Cartesian mode, with `ir_wavenumbers_cm` to match
    bands) and `dipole_derivatives` (in-process; intensities are derived once modes are known) is
    populated, and which one says which backend ran.
    """

    matrix: np.ndarray
    electronic_energy_hartree: float
    # Largest absolute gradient component at the undisplaced geometry, in Hartree/Angstrom: evidence
    # of whether this is a stationary point. `None` on the binary backend. Evidence, not a gate,
    # since a Hessian at a transition state or scan point is legitimate; downstream thermochemistry
    # drops non-positive modes and cannot tell otherwise.
    max_gradient: float | None = None
    ir_intensities: np.ndarray | None = None
    # Wavenumber (cm^-1) of each `ir_intensities` entry, same order — negative for imaginary, zero
    # for a projected-out mode. `None` whenever `ir_intensities` is.
    ir_wavenumbers_cm: np.ndarray | None = None
    dipole_derivatives: np.ndarray | None = None


def _finite_difference(
    spec: HessianSpec, structure: Structure
) -> tuple[np.ndarray, np.ndarray, float, float]:
    """Central-difference Hessian and dipole derivatives at `structure`'s geometry.

    Returns `(hessian, dipole_derivatives, energy, max_gradient)`: the Hessian in
    Hartree/Angstrom^2, shape (3N, 3N); dipole derivatives in Debye/Angstrom, shape (3N, 3); the
    energy at the undisplaced geometry; and the largest analytic gradient component there (already
    computed, so free).

    6N + 1 single points, since the gradient is analytic. The Hessian is symmetrized afterwards to
    remove the small asymmetry that would put spurious imaginary components into the eigenvalues.
    """
    numbers, positions = structure.arrays()
    calc = make_calculator(
        spec.method,
        numbers,
        positions,
        charge=structure.charge,
        uhf=structure.uhf,
        solvent=spec.solvent,
    )
    size = positions.size
    hessian = np.zeros((size, size))
    dipole_derivatives = np.zeros((size, 3))
    step = spec.displacement_angstrom
    deadline = Deadline(settings.xtb_inline_timeout_seconds)
    energy, gradient, _ = evaluate_point(calc, positions)
    for index in range(size):
        # Checked between displacements, the finest granularity: a single point is not interruptible
        # and the worker thread does not stop when the caller gives up.
        deadline.check("Hessian")
        shifted = positions.copy().ravel()
        shifted[index] += step
        _, gradient_plus, dipole_plus = evaluate_point(calc, shifted.reshape(-1, 3))
        shifted[index] -= 2 * step
        _, gradient_minus, dipole_minus = evaluate_point(calc, shifted.reshape(-1, 3))
        hessian[index] = (gradient_plus.ravel() - gradient_minus.ravel()) / (2 * step)
        dipole_derivatives[index] = (dipole_plus - dipole_minus) * AU_TO_DEBYE / (2 * step)
    max_gradient = float(np.max(np.abs(gradient)))
    return 0.5 * (hessian + hessian.T), dipole_derivatives, energy, max_gradient


def compute_hessian(spec: HessianSpec, structure: Structure) -> Hessian:
    """The second derivatives at `structure`.

    The `xtb` binary computes the Hessian and IR intensities itself, much faster than finite
    differences; thermochemistry stays in `xtb_thermo` so both backends get identical treatment.
    Returns the gradient at the undisplaced geometry, since non-stationary geometries are accepted.

    Raises `ValueError` above `settings.xtb_hessian_max_atoms`, or if the in-process run exceeds
    `settings.xtb_inline_timeout_seconds`. The refusal states this server's limit and names no
    alternative route: Chemclaw3 owns that knowledge and refuses first; this is the backstop.
    """
    if len(structure.elements) > settings.xtb_hessian_max_atoms:
        raise ValueError(
            f"a Hessian on {len(structure.elements)} atoms exceeds this server's inline limit of "
            f"{settings.xtb_hessian_max_atoms}: the cost is 6N single points and a tool call runs "
            "inside a conversation turn"
        )
    resolved = spec.for_structure(structure)
    if resolved.engine == "xtb":
        outcome = xtb_cli.run(
            structure,
            task="hess",
            method=resolved.method,
            solvent=resolved.solvent,
            accuracy=resolved.accuracy,
        )
        return Hessian(
            matrix=np.asarray(outcome.hessian),
            electronic_energy_hartree=outcome.energy_hartree,
            ir_intensities=np.asarray(outcome.ir_intensities),
            ir_wavenumbers_cm=np.asarray(outcome.ir_wavenumbers_cm),
        )

    matrix, dipole_derivatives, energy, max_gradient = _finite_difference(spec, structure)
    return Hessian(
        matrix=matrix,
        electronic_energy_hartree=energy,
        max_gradient=max_gradient,
        dipole_derivatives=dipole_derivatives,
    )


def pack_array(array: np.ndarray) -> str:
    """Serialize a float array as base64-encoded `.npy` — how a Hessian crosses the wire.

    `.npy` round-trips float64 exactly, carries shape and dtype (a truncated payload fails to load),
    and is what Chemclaw3's artifact store holds, so the bytes can be stored as received.
    """
    buffer = io.BytesIO()
    np.save(buffer, array, allow_pickle=False)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def unpack_array(encoded: str) -> np.ndarray:
    """Read a `pack_array` payload back into an array.

    `allow_pickle=False`: these bytes come off a wire, and unpickling is arbitrary code execution.
    """
    return np.asarray(np.load(io.BytesIO(base64.b64decode(encoded)), allow_pickle=False))
